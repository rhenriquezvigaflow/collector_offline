from fastapi.testclient import TestClient

from collector_offline.status_server import create_status_app


def test_fastapi_status_surface() -> None:
    client = TestClient(create_status_app(lambda: {"state": "online", "backlog": {"pending": 0}}))

    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").status_code == 200
    assert client.get("/status").json()["state"] == "online"
