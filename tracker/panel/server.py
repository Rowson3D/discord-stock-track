"""Run the panel's uvicorn server inside the tracker's asyncio loop."""

from __future__ import annotations

import contextlib
import logging
import socket
import sys
from collections.abc import Iterator

import uvicorn
from fastapi import FastAPI

from tracker.settings import PanelSettings

logger = logging.getLogger(__name__)


class _EmbeddedServer(uvicorn.Server):
    """uvicorn without its own signal handling: the tracker owns shutdown."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield

    def install_signal_handlers(self) -> None:  # Older uvicorn versions.
        return None


class PanelServer:
    def __init__(self, app: FastAPI, settings: PanelSettings) -> None:
        self.settings = settings
        config = uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
            server_header=False,
            date_header=False,
            proxy_headers=False,  # Never trust X-Forwarded-*; Tailscale headers are explicit.
            ws="none",
        )
        self._server = _EmbeddedServer(config)
        self._socket: socket.socket | None = None

    @property
    def url(self) -> str:
        host = f"[{self.settings.host}]" if ":" in self.settings.host else self.settings.host
        return f"http://{host}:{self.settings.port}"

    def bind(self) -> bool:
        """Claim the port up front, so a busy port disables the panel, not the tracker."""
        family = socket.AF_INET6 if ":" in self.settings.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        if sys.platform != "win32":  # On Windows this would allow port hijacking.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.settings.host, self.settings.port))
        except OSError as error:
            sock.close()
            logger.error("Owner panel disabled: can't bind %s (%s)", self.url, error)
            return False
        self._socket = sock
        return True

    async def serve(self) -> None:
        if self._socket is None and not self.bind():
            return
        assert self._socket is not None
        try:
            await self._server.serve(sockets=[self._socket])
        except Exception:
            logger.exception("Owner panel crashed (the tracker keeps running)")
        finally:
            self._socket.close()

    def stop(self) -> None:
        self._server.should_exit = True
