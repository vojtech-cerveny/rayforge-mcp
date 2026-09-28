"""Line-delimited JSON over TCP, bound to localhost only.

Each request is one line: {"id": ..., "method": "...", "params": {...}}.
Each response is one line: {"id": ..., "result": ...} or
{"id": ..., "error": "..."}.
"""

import json
import logging
import socketserver
import threading
import traceback
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

Dispatch = Callable[[str, dict[str, Any]], Any]


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        dispatch: Dispatch = self.server.dispatch  # type: ignore[attr-defined]
        for raw in self.rfile:
            line = raw.strip()
            if not line:
                continue
            response = self._process(dispatch, line)
            payload = json.dumps(response, default=str) + "\n"
            self.wfile.write(payload.encode("utf-8"))
            self.wfile.flush()

    @staticmethod
    def _process(dispatch: Dispatch, line: bytes) -> dict[str, Any]:
        req_id = None
        try:
            request = json.loads(line)
            req_id = request.get("id")
            method = request["method"]
            params = request.get("params") or {}
            result = dispatch(method, params)
            return {"id": req_id, "result": result}
        except Exception as e:  # noqa: BLE001 - reported to the client
            logger.warning(f"MCP bridge request failed: {e}")
            logger.debug(traceback.format_exc())
            return {"id": req_id, "error": f"{type(e).__name__}: {e}"}


class _TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class BridgeServer:
    def __init__(self, dispatch: Dispatch, port: int):
        self._dispatch = dispatch
        self._port = port
        self._server: _TCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        server = _TCPServer(("127.0.0.1", self._port), _Handler)
        server.dispatch = self._dispatch  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="mcp-bridge",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"MCP bridge listening on 127.0.0.1:{self._port}")

    def stop(self):
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
        logger.info("MCP bridge stopped")
