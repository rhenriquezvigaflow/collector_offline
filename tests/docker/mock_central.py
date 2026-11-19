"""Disposable HTTPS backfill fixture; cannot access production networks."""

from __future__ import annotations

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

lock = threading.Lock()
state: dict[str, Any] = {
    "ready": True,
    "lose_ack": True,
    "posts": 0,
    "duplicates": 0,
    "minutes": {},
    "events": {},
    "forbidden": 0,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def respond(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        with lock:
            if self.path == "/metrics":
                self.respond(
                    200,
                    {
                        **state,
                        "minutes": len(state["minutes"]),
                        "events": len(state["events"]),
                        "value_sum": sum(
                            row.get("value_num") or 0 for row in state["minutes"].values()
                        ),
                    },
                )
            elif self.path == "/health/ready":
                self.respond(
                    200 if state["ready"] else 503,
                    {"status": "ready", "checks": {"database": "ok"}},
                )
            else:
                state["forbidden"] += 1
                self.respond(404, {})

    def do_POST(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        with lock:
            if self.path == "/control":
                state.update({k: payload[k] for k in ("ready", "lose_ack") if k in payload})
                self.respond(200, {"ok": True})
                return
            if self.path != "/ingest/offline/v1/backfill":
                state["forbidden"] += 1
                self.respond(404, {})
                return
            if self.headers.get("Authorization") != "Bearer isolated-test-token-never-production":
                self.respond(403, {})
                return
            if (payload.get("source_node_id"), payload.get("lagoon_id")) != (
                "edge-docker-test",
                "docker-test",
            ):
                self.respond(403, {})
                return
            if not state["ready"]:
                self.respond(503, {})
                return
            state["posts"] += 1
            results = []
            for kind, collection in (("minute", "minutes"), ("event", "events")):
                for row in payload[collection]:
                    key = (
                        f"{payload['lagoon_id']}:{row['tag_id']}:{row['bucket']}"
                        if kind == "minute"
                        else row["local_id"]
                    )
                    duplicate = key in state[collection]
                    state["duplicates"] += int(duplicate)
                    state[collection][key] = row
                    results.append(
                        {
                            "kind": kind,
                            "local_id": row["local_id"],
                            "status": "duplicate" if duplicate else "accepted",
                        }
                    )
            if state["lose_ack"]:
                state["lose_ack"] = False
                self.respond(503, {})  # Central committed, acknowledgement lost.
            else:
                self.respond(200, {"results": results})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8090), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/tls/central.crt", "/tls/central.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
