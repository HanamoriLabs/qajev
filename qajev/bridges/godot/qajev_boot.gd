extends SceneTree
# QAJev boot for Godot games. It lives in QAJev, not in the game: the game's own main scene runs unchanged, and
# QAJev's bridge node is added next to it.
#   godot --path <project> --user-data-dir <throwaway dir> --script <this file>
# env: QAJEV_BRIDGE_PORT (required), QAJEV_BRIDGE_SCRIPT (this folder's qajev_bridge.gd),
#      QAJEV_ADAPTER (optional: a per-game adapter script that describes custom-drawn screens).

func _init() -> void:
	call_deferred("_run")

func _run() -> void:
	var main_scene: String = ProjectSettings.get_setting("application/run/main_scene")
	change_scene_to_file(main_scene)
	await process_frame
	await process_frame
	var bridge: Node = load(OS.get_environment("QAJEV_BRIDGE_SCRIPT")).new()
	bridge.name = "QAJevBridge"
	root.add_child(bridge)
