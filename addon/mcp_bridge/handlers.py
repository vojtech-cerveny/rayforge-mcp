"""Request handlers for the MCP bridge.

Handlers run on a socket thread. Anything that reads or changes the
document goes through ``on_main`` because the document fires blinker
signals that GTK widgets listen to. Every change goes through the
editor's history manager, so Ctrl+Z in Rayforge undoes it.
"""

import asyncio
import base64
import mimetypes
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gi.repository import GLib

from rayforge.core.step_registry import step_registry
from rayforge.core.undo import Command
from rayforge.core.undo.list_cmd import ListItemCommand
from rayforge.core.vectorization_spec import PassthroughSpec, TraceSpec
from rayforge.core.layer import Layer
from rayforge.core.workpiece import WorkPiece

from .preview import snapshot_widget_png

MAIN_THREAD_TIMEOUT = 60.0
SETTLE_TIMEOUT = 60.0
MULTIPASS = "MultiPassTransformer"


def on_main(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run func on the GTK main thread and return its result."""
    done = threading.Event()
    box: dict[str, Any] = {}

    def run():
        try:
            box["result"] = func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - re-raised on caller thread
            box["error"] = e
        finally:
            done.set()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(run)
    if not done.wait(MAIN_THREAD_TIMEOUT):
        raise TimeoutError("Rayforge main thread did not respond")
    if "error" in box:
        raise box["error"]
    return box.get("result")


class _RecipeValueCommand(Command):
    """Undoable change of one recipe-eligible step attribute."""

    def __init__(self, step, key: str, new_value: Any, old_value: Any):
        super().__init__(name=f"Set {key}")
        self._step = step
        self._key = key
        self._new = new_value
        self._old = old_value

    def execute(self) -> None:
        self._apply(self._new)

    def undo(self) -> None:
        self._apply(self._old)

    def _apply(self, value: Any) -> None:
        has_setter = self._step.get_recipe_setter_name(self._key)
        self._step.set_recipe_value(self._key, value)
        if not has_setter:
            self._step.updated.send(self._step)


class _DictValueCommand(Command):
    """Undoable change of a transformer dict value on a step."""

    def __init__(self, step, target: dict, key: str, new_value: Any):
        super().__init__(name=f"Set {key}")
        self._step = step
        self._target = target
        self._key = key
        self._new = new_value
        self._old = target.get(key)

    def execute(self) -> None:
        self._apply(self._new)

    def undo(self) -> None:
        self._apply(self._old)

    def _apply(self, value: Any) -> None:
        self._target[self._key] = value
        if self._target in self._step.per_step_transformers_dicts:
            self._step.per_step_transformer_changed.send(self._step)
        else:
            self._step.updated.send(self._step)


class Handlers:
    def __init__(self, main_window):
        self._win = main_window
        self._methods: dict[str, Callable[[dict[str, Any]], Any]] = {
            "ping": self.ping,
            "get_document": self.get_document,
            "import_file": self.import_file,
            "transform": self.transform,
            "delete_items": self.delete_items,
            "add_layer": self.add_layer,
            "set_active_layer": self.set_active_layer,
            "move_to_layer": self.move_to_layer,
            "list_step_types": self.list_step_types,
            "add_step": self.add_step,
            "remove_step": self.remove_step,
            "describe_step": self.describe_step,
            "set_step": self.set_step,
            "list_recipes": self.list_recipes,
            "apply_recipe": self.apply_recipe,
            "preview": self.preview,
            "save_project": self.save_project,
            "undo": self.undo,
            "redo": self.redo,
        }

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        handler = self._methods.get(method)
        if handler is None:
            raise ValueError(f"Unknown method '{method}'")
        return handler(params)

    @property
    def _editor(self):
        return self._win.doc_editor

    @property
    def _doc(self):
        return self._editor.doc

    @property
    def _history(self):
        return self._editor.history_manager

    def _find(self, uid: str, kind: type | None = None):
        item = self._doc.find_descendant_by_uid(uid)
        if item is None:
            raise ValueError(f"No item with uid '{uid}'")
        if kind is not None and not isinstance(item, kind):
            raise ValueError(f"Item '{uid}' is not a {kind.__name__}")
        return item

    def _find_step(self, uid: str):
        for layer in self._doc.layers:
            workflow = layer.workflow
            if not workflow:
                continue
            for step in workflow.steps:
                if step.uid == uid:
                    return step
        raise ValueError(f"No step with uid '{uid}'")

    def _find_many(self, uids: list[str]) -> list:
        if not uids:
            raise ValueError("No uids given")
        return [self._find(uid) for uid in uids]

    def _wait_settled(self) -> bool:
        return self._editor.wait_until_settled_sync(SETTLE_TIMEOUT)

    # Serialization

    def _item_info(self, item) -> dict[str, Any]:
        transform = self._editor.transform
        x, y = transform.get_position_group([item]) or (0.0, 0.0)
        w, h = transform.get_size_group([item]) or (0.0, 0.0)
        info: dict[str, Any] = {
            "uid": item.uid,
            "name": item.name,
            "type": type(item).__name__,
            "x": round(x, 3),
            "y": round(y, 3),
            "width": round(w, 3),
            "height": round(h, 3),
            "angle": round(item.angle, 3),
        }
        if isinstance(item, WorkPiece) and item.source_file:
            info["source_file"] = str(item.source_file)
        return info

    @staticmethod
    def _passes_dict(step) -> dict | None:
        for d in list(step.per_step_transformers_dicts) + list(
            step.per_workpiece_transformers_dicts
        ):
            if d.get("name") == MULTIPASS:
                return d
        return None

    def _step_info(self, step) -> dict[str, Any]:
        info: dict[str, Any] = {
            "uid": step.uid,
            "type": type(step).__name__,
            "name": step.name,
            "visible": step.visible,
            "summary": step.get_summary(),
        }
        for key in ("power", "cut_speed", "travel_speed", "air_assist"):
            if hasattr(step, key):
                info[key] = step.recipe_value(key, getattr(step, key))
        passes = self._passes_dict(step)
        if passes is not None:
            info["passes"] = passes.get("passes", 1)
        return info

    def _layer_info(self, layer) -> dict[str, Any]:
        workflow = layer.workflow
        steps = list(workflow.steps) if workflow else []
        return {
            "uid": layer.uid,
            "name": layer.name,
            "active": layer is self._doc.active_layer,
            "visible": layer.visible,
            "items": [self._item_info(i) for i in layer.content_items],
            "steps": [self._step_info(s) for s in steps],
        }

    def _machine_info(self) -> dict[str, Any] | None:
        machine = self._editor.context.machine
        if not machine:
            return None
        width, height = machine.axis_extents
        return {
            "name": machine.name,
            "work_area_mm": [width, height],
            "x_axis_right": machine.x_axis_right,
            "y_axis_down": machine.y_axis_down,
        }

    # Methods

    def ping(self, params):
        return {"ok": True}

    def get_document(self, params):
        def read():
            doc = self._doc
            return {
                "file": str(self._editor.file_path or ""),
                "saved": self._editor.is_saved,
                "machine": self._machine_info(),
                "layers": [self._layer_info(layer) for layer in doc.layers],
                "stock": [
                    {"uid": s.uid, "name": s.name} for s in doc.stock_items
                ],
            }

        return on_main(read)

    def import_file(self, params):
        path = Path(params["path"]).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        mode = params.get("mode", "auto")
        spec = {
            "auto": None,
            "vector": PassthroughSpec(),
            "trace": TraceSpec(),
        }[mode]
        mime, _ = mimetypes.guess_type(path)
        file_cmd = self._editor.file

        def top_level_uids() -> set[str]:
            return {
                item.uid
                for layer in self._doc.layers
                for item in layer.content_items
            }

        def prepare():
            layer_uid = params.get("layer_uid")
            if layer_uid:
                layer = self._find(layer_uid, Layer)
                self._editor.layer.set_active_layer(layer)
            return top_level_uids()

        before = on_main(prepare)
        result = asyncio.run(file_cmd._load_file_async(path, mime, spec))
        if not result or not result.payload:
            raise ValueError(f"Importer produced no items for {path.name}")

        def finalize():
            file_cmd._finalize_import_on_main_thread(
                result.payload, path, None, spec
            )
            return [uid for uid in top_level_uids() if uid not in before]

        new_uids = on_main(finalize)
        placement = {
            k: params[k]
            for k in ("x", "y", "width", "height", "angle", "keep_ratio")
            if params.get(k) is not None
        }
        if new_uids and placement:
            self.transform({"uids": new_uids, **placement})
        self._wait_settled()
        return on_main(
            lambda: [self._item_info(self._find(uid)) for uid in new_uids]
        )

    def transform(self, params):
        def apply():
            items = self._find_many(params["uids"])
            transform = self._editor.transform
            width = params.get("width")
            height = params.get("height")
            keep_ratio = params.get("keep_ratio", True)
            if width is not None or height is not None:
                transform.set_size_group(
                    items, width, height, fixed_ratio=keep_ratio
                )
            angle = params.get("angle")
            if angle is not None:
                transform.set_angle_group(items, float(angle))
            x = params.get("x")
            y = params.get("y")
            if x is not None or y is not None:
                cur_x, cur_y = transform.get_position_group(items)
                transform.set_position_group(
                    items,
                    cur_x if x is None else float(x),
                    cur_y if y is None else float(y),
                )
            return [self._item_info(i) for i in items]

        return on_main(apply)

    def delete_items(self, params):
        def apply():
            items = self._find_many(params["uids"])
            self._editor.edit.remove_items(items)
            return {"deleted": len(items)}

        return on_main(apply)

    def add_layer(self, params):
        def apply():
            layer = Layer(params.get("name") or "Layer")
            self._editor.layer.add_layer_and_set_active(layer)
            return self._layer_info(layer)

        return on_main(apply)

    def set_active_layer(self, params):
        def apply():
            layer = self._find(params["layer_uid"], Layer)
            self._editor.layer.set_active_layer(layer)
            return {"active": layer.uid}

        return on_main(apply)

    def move_to_layer(self, params):
        def apply():
            items = self._find_many(params["uids"])
            layer = self._find(params["layer_uid"], Layer)
            self._editor.layer.move_items_to_layer(items, layer)
            return self._layer_info(layer)

        return on_main(apply)

    def list_step_types(self, params):
        def read():
            return [
                {
                    "type": name,
                    "label": cls.TYPELABEL,
                    "hidden": cls.HIDDEN,
                }
                for name, cls in step_registry.all_steps().items()
            ]

        return on_main(read)

    def add_step(self, params):
        def apply():
            layer = self._find(params["layer_uid"], Layer)
            cls = step_registry.get(params["type"])
            if cls is None:
                raise ValueError(f"Unknown step type '{params['type']}'")
            step = cls.create(self._editor.context)
            self._editor.step.apply_best_recipe_to_step(step)
            self._history.execute(
                ListItemCommand(
                    owner_obj=layer.workflow,
                    item=step,
                    undo_command="remove_step",
                    redo_command="add_step",
                    name=f"Add step '{step.name}'",
                )
            )
            return self._step_info(step)

        return on_main(apply)

    def remove_step(self, params):
        def apply():
            step = self._find_step(params["step_uid"])
            self._history.execute(
                ListItemCommand(
                    owner_obj=step.workflow,
                    item=step,
                    undo_command="add_step",
                    redo_command="remove_step",
                    name=f"Remove step '{step.name}'",
                )
            )
            return {"removed": step.uid}

        return on_main(apply)

    def describe_step(self, params):
        def read():
            step = self._find_step(params["step_uid"])
            settings = []
            for var in type(step).recipe_varset():
                entry = var.to_dict()
                entry["value"] = step.recipe_value(
                    var.key, getattr(step, var.key, None)
                )
                for attr in ("min_val", "max_val", "choices"):
                    if hasattr(var, attr):
                        entry[attr] = getattr(var, attr)
                settings.append(entry)
            passes = self._passes_dict(step)
            return {
                **self._step_info(step),
                "settings": settings,
                "passes_available": passes is not None,
            }

        return on_main(read)

    def _coerce(self, step, key: str, value: Any) -> Any:
        var = type(step).recipe_varset().get(key)
        if var is None:
            return value
        try:
            var.value = value
        except TypeError:
            return value
        var.validate()
        return var.value

    def _step_commands(self, step, settings: dict[str, Any]) -> list:
        allowed = set(step.recipe_keys())
        commands: list[Command] = []
        for key, value in settings.items():
            if key == "passes":
                target = self._passes_dict(step)
                if target is None:
                    raise ValueError(f"Step '{step.name}' has no passes")
                passes = int(value)
                if passes < 1:
                    raise ValueError("passes must be at least 1")
                commands.append(
                    _DictValueCommand(step, target, "passes", passes)
                )
                continue
            if key not in allowed:
                raise ValueError(
                    f"'{key}' is not a setting of {type(step).__name__}. "
                    f"Allowed: {sorted(allowed | {'passes'})}"
                )
            new_value = self._coerce(step, key, value)
            old_value = step.recipe_value(key, getattr(step, key, None))
            commands.append(
                _RecipeValueCommand(step, key, new_value, old_value)
            )
        return commands

    def _apply_settings(self, step, settings: dict[str, Any], name: str):
        commands = self._step_commands(step, settings)
        with self._history.transaction(name) as t:
            for command in commands:
                t.execute(command)

    def set_step(self, params):
        def apply():
            step = self._find_step(params["step_uid"])
            settings = dict(params.get("settings") or {})
            name = params.get("name")
            if name:
                self._editor.step.rename_step(step, name)
            if settings:
                self._apply_settings(step, settings, "Change step settings")
            return self._step_info(step)

        return on_main(apply)

    def list_recipes(self, params):
        def read():
            mgr = self._editor.context.recipe_mgr
            if mgr is None:
                return []
            return [
                {
                    "uid": r.uid,
                    "name": r.name,
                    "description": r.description,
                    "step_types": r.target_step_types,
                    "material_uid": r.material_uid,
                    "min_thickness_mm": r.min_thickness_mm,
                    "max_thickness_mm": r.max_thickness_mm,
                    "settings": r.get_applied_settings(),
                }
                for r in mgr.get_all_recipes()
            ]

        return on_main(read)

    def apply_recipe(self, params):
        def apply():
            step = self._find_step(params["step_uid"])
            mgr = self._editor.context.recipe_mgr
            recipe = mgr.get_recipe_by_id(params["recipe_uid"]) if mgr else None
            if recipe is None:
                raise ValueError(f"No recipe '{params['recipe_uid']}'")
            settings = recipe.get_settings_for_step(step)
            self._apply_settings(
                step, settings, f"Apply recipe '{recipe.name}'"
            )
            step.applied_recipe_uid = recipe.uid
            return self._step_info(step)

        return on_main(apply)

    def preview(self, params):
        settled = self._wait_settled()
        png = on_main(snapshot_widget_png, self._win.surface)
        return {
            "png_base64": base64.b64encode(png).decode("ascii"),
            "settled": settled,
        }

    def save_project(self, params):
        path = Path(params["path"]).expanduser().resolve()
        if path.suffix != ".ryp":
            path = path.with_suffix(".ryp")

        def apply():
            self._editor.file.save_project_to_path(path)
            return {"path": str(path)}

        return on_main(apply)

    def undo(self, params):
        return on_main(self._history_step, "undo")

    def redo(self, params):
        return on_main(self._history_step, "redo")

    def _history_step(self, direction: str):
        history = self._history
        can = history.can_undo() if direction == "undo" else history.can_redo()
        if not can:
            return {"done": False}
        getattr(history, direction)()
        return {"done": True}
