import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Graphene, Gtk  # noqa: E402


def snapshot_widget_png(widget: Gtk.Widget) -> bytes:
    """Render a mapped widget to PNG bytes. Call on the main thread."""
    width = widget.get_width()
    height = widget.get_height()
    if width <= 0 or height <= 0:
        raise RuntimeError("Canvas is not visible")
    paintable = Gtk.WidgetPaintable.new(widget)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        raise RuntimeError("Canvas rendered nothing")
    renderer = widget.get_native().get_renderer()
    bounds = Graphene.Rect().init(0, 0, width, height)
    texture = renderer.render_texture(node, bounds)
    return texture.save_to_png_bytes().get_data()
