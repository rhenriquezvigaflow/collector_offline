from __future__ import annotations

import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from conftest import SequenceReader, enter_offline, health

from collector_offline.application.collector import LocalCollector
from collector_offline.application.sync_worker import SyncWorker
from collector_offline.config import CentralConfig, SupervisorConfig, SyncConfig, TagSpec
from collector_offline.infrastructure.drivers import BoundReader
from collector_offline.infrastructure.http_client import CentralClient


def test_real_http_lost_ack_retries_as_duplicate(repository, base_time) -> None:
    received = set()
    requests_seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_POST(self) -> None:
            assert self.path == "/ingest/offline/v1/backfill"
            assert self.headers["Authorization"] == "Bearer isolated-test-token"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests_seen.append(body)
            results = []
            for row in body["minutes"]:
                key = (body["lagoon_id"], row["tag_id"], row["bucket"])
                results.append(
                    {
                        "kind": "minute",
                        "local_id": row["local_id"],
                        "status": "duplicate" if key in received else "accepted",
                    }
                )
                received.add(key)
            encoded = json.dumps({"results": results}).encode()
            self.send_response(503 if len(requests_seen) == 1 else 200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = CentralClient(
        CentralConfig(
            health_url=f"http://127.0.0.1:{server.server_port}/health/ready",
            sync_url=f"http://127.0.0.1:{server.server_port}/ingest/offline/v1/backfill",
        ),
        "isolated-test-token",
        "edge-test-01",
        "small_sim",
        "small",
    )
    try:
        enter_offline(repository, base_time)
        reader = SequenceReader([{"TEMP": 20.0}])
        LocalCollector(
            repository, [BoundReader("sim", reader, {"TEMP": TagSpec(address="TEMP")})], "small_sim"
        ).capture(base_time)
        repository.process_health(
            health(True, base_time + timedelta(minutes=1)), SupervisorConfig(recovery_threshold=1)
        )
        worker = SyncWorker(repository, client, SyncConfig(backoff_base_sec=0.1, backoff_max_sec=1))
        assert worker.run_once(base_time + timedelta(minutes=2))["pending"] == 1
        assert worker.run_once(base_time + timedelta(minutes=3))["duplicate"] == 1
        assert worker.run_once(base_time + timedelta(minutes=4))["sync_completed"] == 1
        assert requests_seen[0] == requests_seen[1]
        assert len(received) == 1
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
