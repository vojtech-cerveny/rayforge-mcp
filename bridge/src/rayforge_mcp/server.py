"""MCP server for Rayforge.

Forwards tool calls over a localhost socket to the mcp_bridge addon
running inside Rayforge. Rayforge must be open with the addon enabled.
This server never controls the machine: the user starts jobs in Rayforge.
"""

import base64
import itertools
import json
import os
import socket
import threading
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP, Image

HOST = "127.0.0.1"
PORT = int(os.environ.get("RAYFORGE_MCP_PORT", "9877"))
TIMEOUT = 120.0

INSTRUCTIONS = """\
Edits the document open in Rayforge, a laser cutter and engraver app.
Units: millimetres. Positions (x, y) are machine coordinates of the
item's bounding box corner nearest the machine origin, the same values
Rayforge shows in its properties panel. Call get_document first to see
the work area, layers, items and steps.

Operations live on layers. Each layer has a workflow of steps
(ContourStep cuts or scores outlines, EngraveStep fills/rasters).
Every item on a layer gets all steps of that layer. To cut some shapes
and engrave others, put them on separate layers.

Step settings: power is a fraction 0.0-1.0 (0.3 = 30 %), cut_speed and
travel_speed are mm/min, passes is an integer. Call describe_step for
the full list of settings a step accepts.

Every change is undoable in Rayforge (Ctrl+Z) or with the undo tool.
Call preview after changes to check the sheet visually. This server
cannot start, frame or move the laser; the user does that in Rayforge.
"""

mcp = FastMCP("rayforge", instructions=INSTRUCTIONS)


class BridgeError(RuntimeError):
    pass


class BridgeClient:
    def __init__(self, host: str, port: int):
        self._addr = (host, port)
        self._sock: socket.socket | None = None
        self._reader = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def _connect(self):
        try:
            sock = socket.create_connection(self._addr, timeout=5.0)
        except OSError as e:
            raise BridgeError(
                f"Cannot reach Rayforge on {self._addr[0]}:{self._addr[1]}. "
                "Open Rayforge and enable the MCP Bridge addon."
            ) from e
        sock.settimeout(TIMEOUT)
        self._sock = sock
        self._reader = sock.makefile("rb")

    def _close(self):
        if self._sock is not None:
            self._sock.close()
        self._sock = None
        self._reader = None

    def call(self, method: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        with self._lock:
            for attempt in range(2):
                if self._sock is None:
                    self._connect()
                try:
                    return self._roundtrip(method, params)
                except (OSError, ConnectionError):
                    self._close()
                    if attempt == 1:
                        raise
        raise BridgeError("unreachable")

    def _roundtrip(self, method: str, params: dict[str, Any]) -> Any:
        assert self._sock is not None and self._reader is not None
        req_id = next(self._ids)
        line = json.dumps({"id": req_id, "method": method, "params": params})
        self._sock.sendall(line.encode("utf-8") + b"\n")
        raw = self._reader.readline()
        if not raw:
            raise ConnectionError("Rayforge closed the connection")
        response = json.loads(raw)
        if "error" in response:
            raise BridgeError(response["error"])
        return response.get("result")


bridge = BridgeClient(HOST, PORT)


@mcp.tool()
def get_document() -> dict:
    """Machine work area, layers with their items and steps, and stock."""
    return bridge.call("get_document")


@mcp.tool()
def import_file(
    path: str,
    mode: Literal["auto", "vector", "trace"] = "auto",
    layer_uid: str | None = None,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    angle: float | None = None,
    keep_ratio: bool = True,
) -> list:
    """Import an SVG, PNG, JPG, DXF or PDF file into the open document.

    mode: auto lets Rayforge decide (SVG as vectors, bitmaps as images),
    vector forces direct vector import, trace converts a bitmap to
    outlines. layer_uid picks the target layer, default is the active
    one. Rayforge adds default steps to a layer that has none, and for
    bitmaps that default is a Contour step, so add an EngraveStep and
    remove the Contour when the image should be engraved.
    x, y, width, height (mm) and angle (degrees) place the result;
    with keep_ratio, giving only width or height keeps proportions.
    Returns the new items.
    """
    return bridge.call(
        "import_file",
        path=path,
        mode=mode,
        layer_uid=layer_uid,
        x=x,
        y=y,
        width=width,
        height=height,
        angle=angle,
        keep_ratio=keep_ratio,
    )


@mcp.tool()
def transform(
    uids: list[str],
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    angle: float | None = None,
    keep_ratio: bool = True,
) -> list:
    """Move, resize or rotate items. Several uids act as one group.

    Size is applied first, then rotation, then position. Omitted
    values stay unchanged.
    """
    return bridge.call(
        "transform",
        uids=uids,
        x=x,
        y=y,
        width=width,
        height=height,
        angle=angle,
        keep_ratio=keep_ratio,
    )


@mcp.tool()
def delete_items(uids: list[str]) -> dict:
    """Delete items from the document."""
    return bridge.call("delete_items", uids=uids)


@mcp.tool()
def add_layer(name: str) -> dict:
    """Add an empty layer and make it active. Add steps with add_step."""
    return bridge.call("add_layer", name=name)


@mcp.tool()
def set_active_layer(layer_uid: str) -> dict:
    """Make a layer active, so imports land on it."""
    return bridge.call("set_active_layer", layer_uid=layer_uid)


@mcp.tool()
def move_to_layer(uids: list[str], layer_uid: str) -> dict:
    """Move items to another layer."""
    return bridge.call("move_to_layer", uids=uids, layer_uid=layer_uid)


@mcp.tool()
def list_step_types() -> list:
    """Step types that add_step accepts, e.g. ContourStep, EngraveStep."""
    return bridge.call("list_step_types")


@mcp.tool()
def add_step(layer_uid: str, type: str) -> dict:
    """Add a step to a layer's workflow. Rayforge applies the best
    matching recipe, then you can adjust it with set_step."""
    return bridge.call("add_step", layer_uid=layer_uid, type=type)


@mcp.tool()
def remove_step(step_uid: str) -> dict:
    """Remove a step from its layer."""
    return bridge.call("remove_step", step_uid=step_uid)


@mcp.tool()
def describe_step(step_uid: str) -> dict:
    """All settings of a step with current value, range and meaning."""
    return bridge.call("describe_step", step_uid=step_uid)


@mcp.tool()
def set_step(
    step_uid: str,
    settings: dict[str, Any] | None = None,
    name: str | None = None,
) -> dict:
    """Change step settings, as one undo entry.

    settings example: {"power": 0.3, "cut_speed": 3000, "passes": 2}.
    Keys come from describe_step. power is 0.0-1.0, speeds are mm/min.
    """
    return bridge.call(
        "set_step", step_uid=step_uid, settings=settings, name=name
    )


@mcp.tool()
def list_recipes() -> list:
    """Saved material recipes (power, speed and more per material)."""
    return bridge.call("list_recipes")


@mcp.tool()
def apply_recipe(step_uid: str, recipe_uid: str) -> dict:
    """Apply a saved recipe's settings to a step."""
    return bridge.call(
        "apply_recipe", step_uid=step_uid, recipe_uid=recipe_uid
    )


@mcp.tool()
def preview() -> Image:
    """Screenshot of the Rayforge canvas after processing settles."""
    result = bridge.call("preview")
    return Image(data=base64.b64decode(result["png_base64"]), format="png")


@mcp.tool()
def save_project(path: str) -> dict:
    """Save the document as a Rayforge project (.ryp)."""
    return bridge.call("save_project", path=path)


@mcp.tool()
def undo() -> dict:
    """Undo the last change in Rayforge."""
    return bridge.call("undo")


@mcp.tool()
def redo() -> dict:
    """Redo the last undone change in Rayforge."""
    return bridge.call("redo")


def main():
    mcp.run()


if __name__ == "__main__":
    main()
