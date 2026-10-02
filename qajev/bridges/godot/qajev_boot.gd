extends SceneTree
# QAJev boot for Godot games. It lives in QAJev, not in the game: the game's own main scene runs unchanged, and
# QAJev's bridge node is added next to it.
#   HOME=<throwaway dir>/home QAJEV_USER_DIR=<throwaway dir> godot --path <project> --script <this file>
# env: QAJEV_BRIDGE_PORT (required), QAJEV_BRIDGE_SCRIPT (this folder's qajev_bridge.gd),
#      QAJEV_ADAPTER (optional: a per-game adapter script that describes custom-drawn screens),
#      QAJEV_USER_DIR (the throwaway folder user:// must be inside; Godot 4 has no --user-data-dir).

func _init() -> void:
	# Fail closed: the game never runs against the player's real saves.
	var want := OS.get_environment("QAJEV_USER_DIR").trim_suffix("/")
	var have := OS.get_user_data_dir()
	# Inside the folder itself: ".../qajev-native-12-ab" must not accept ".../qajev-native-12-abc/...".
	if want == "" or not (have == want or have.begins_with(want + "/")):
		print("QAJEV_SAVE_NOT_ISOLATED: user:// is ", have)
		quit(3)
		return
	call_deferred("_run")

func _run() -> void:
	var main_scene: String = ProjectSettings.get_setting("application/run/main_scene")
	change_scene_to_file(main_scene)
	await process_frame
	await process_frame
	var bridge: Node = load(OS.get_environment("QAJEV_BRIDGE_SCRIPT")).new()
	bridge.name = "QAJevBridge"
	root.add_child(bridge)
