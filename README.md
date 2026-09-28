# rayforge-mcp

An MCP server that lets Claude (or any MCP client) edit the document open in
[Rayforge](https://github.com/barebaric/rayforge). You describe what to
engrave, the agent draws or picks an SVG/PNG, imports it, lays out the sheet
and sets power, speed and passes. You check the result in Rayforge and press
Start yourself. The server has no tool that frames, homes, jogs or runs the
laser, and that is on purpose.

It has two parts, the same split as the SketchUp MCP:

- `addon/` is a Rayforge addon (`mcp_bridge`). When the main window opens it
  listens on `127.0.0.1:9877` for line-delimited JSON requests and runs them
  on the GTK main thread through the editor's own command objects. Every
  change lands in Rayforge's undo history.
- `bridge/` is a small stdio MCP server (Python, `mcp` SDK). It forwards each
  tool call to the addon over that socket.

Built against Rayforge 1.11.2 (addon API 21). The import path uses two
private `FileCmd` methods, so check `import_file` after a Rayforge upgrade.

## Install

Link the addon into Rayforge's user addon directory, then restart Rayforge:

```sh
ln -sfn "$PWD/addon" "$HOME/Library/Application Support/rayforge/addons/mcp_bridge"
```

On Linux the directory is `~/.config/rayforge/addons/`.

Register the MCP server with Claude Code:

```sh
claude mcp add rayforge -- uvx --from /path/to/rayforge-mcp/bridge rayforge-mcp
```

Set `RAYFORGE_MCP_PORT` on both sides to use another port.

## Tools

| Tool | What it does |
|---|---|
| `get_document` | Work area, layers, items with position and size, steps with power/speed/passes |
| `import_file` | Import SVG/PNG/JPG/DXF/PDF, optionally onto a layer and at a position and size |
| `transform` | Move, resize, rotate one item or several as a group |
| `delete_items` | Remove items |
| `add_layer`, `set_active_layer`, `move_to_layer` | Split work into layers, one set of steps per layer |
| `list_step_types`, `add_step`, `remove_step` | Contour (cut/score) and Engrave steps per layer |
| `describe_step`, `set_step` | Read and change any step setting, as one undo entry |
| `list_recipes`, `apply_recipe` | Use saved material recipes |
| `preview` | PNG screenshot of the Rayforge canvas |
| `save_project` | Save as `.ryp` |
| `undo`, `redo` | Rayforge history |

Positions are machine coordinates in mm, the same numbers the Rayforge
properties panel shows. Power is a fraction from 0.0 to 1.0. Speeds are
mm/min.

## Security

The socket binds to localhost only, but any local process can connect to it
and edit the open document. It cannot reach the machine driver.

## License

MIT, same as Rayforge. See [LICENSE](LICENSE).
