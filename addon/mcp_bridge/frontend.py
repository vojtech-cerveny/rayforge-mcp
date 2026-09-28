import logging
import os

from rayforge.core.hooks import hookimpl

from .handlers import Handlers
from .server import BridgeServer

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9877

_server: BridgeServer | None = None


@hookimpl
def main_window_ready(main_window):
    global _server
    if _server is not None:
        return
    port = int(os.environ.get("RAYFORGE_MCP_PORT", DEFAULT_PORT))
    handlers = Handlers(main_window)
    _server = BridgeServer(handlers.dispatch, port=port)
    try:
        _server.start()
    except OSError as e:
        logger.error(f"MCP bridge could not listen on port {port}: {e}")
        _server = None


@hookimpl
def on_unload():
    global _server
    if _server is not None:
        _server.stop()
        _server = None
