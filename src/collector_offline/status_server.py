from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException

logger = logging.getLogger("collector_offline.status")


def create_status_app(status_provider: Callable[[], dict[str, Any]]) -> FastAPI:
    app = FastAPI(
        title="Crystal Collector Offline",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready() -> dict[str, Any]:
        try:
            return {"status": "ready", **status_provider()}
        except Exception as exc:
            raise HTTPException(status_code=503, detail="local database unavailable") from exc

    @app.get("/status")
    def status() -> dict[str, Any]:
        try:
            return status_provider()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="local status unavailable") from exc

    return app


class StatusServer:
    def __init__(
        self,
        host: str,
        port: int,
        status_provider: Callable[[], dict[str, Any]],
    ) -> None:
        self._config = uvicorn.Config(
            create_status_app(status_provider),
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(
            target=self._server.run,
            name="fastapi-status-server",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        logger.info(
            "Local FastAPI status server started",
            extra={"host": self._config.host, "port": self._config.port},
        )

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
