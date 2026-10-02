extends SceneTree
# QAJev boot for Godot games. It lives in QAJev, not in the game: the game's own main scene runs unchanged, and
# QAJev's bridge node is added next to it.
#   HOME=<throwaway dir>/home QAJEV_USER_DIR=<throwaway dir> godot --path <project> --script <this file>
# env: QAJEV_BRIDGE_PORT (required), QAJEV_BRIDGE_SCRIPT (this folder's qajev_bridge.gd),
#      QAJEV_ADAPTER (optional: a per-game adapter script that describes custom-drawn screens),
#      QAJEV_USER_DIR (the throwaway folder user:// must be inside; Godot 4 has no --user-data-dir),
#      QAJEV_SEED_DIR (optional, first launch only: saves to start from, copied into user:// before the game runs).

func _init() -> void:
	# Fail closed: the game never runs against the player's real saves.
	var want := OS.get_environment("QAJEV_USER_DIR").trim_suffix("/")
	var have := OS.get_user_data_dir()
	# Inside the folder itself: ".../qajev-native-12-ab" must not accept ".../qajev-native-12-abc/...".
	if want == "" or not (have == want or have.begins_with(want + "/")):
		print("QAJEV_SAVE_NOT_ISOLATED: user:// is ", have)
		quit(3)
		return
	var seed := OS.get_environment("QAJEV_SEED_DIR")
	if seed != "" and not _copy_tree(seed, have):
		quit(3) # half a seed would test the wrong saves: the game does not start
		return
	call_deferred("_run")

func _copy_tree(from: String, to: String) -> bool:
	DirAccess.make_dir_recursive_absolute(to)
	var dir := DirAccess.open(from)
	if dir == null:
		print("QAJEV_SEED_FAILED: cannot open ", from)
		return false
	for sub in dir.get_directories():
		if not _copy_tree(from.path_join(sub), to.path_join(sub)):
			return false
	for file in dir.get_files():
		if DirAccess.copy_absolute(from.path_join(file), to.path_join(file)) != OK:
			print("QAJEV_SEED_FAILED: cannot copy ", file)
			return false
	return true

func _run() -> void:
	var main_scene: String = ProjectSettings.get_setting("application/run/main_scene")
	change_scene_to_file(main_scene)
	await process_frame
	await process_frame
	var bridge: Node = load(OS.get_environment("QAJEV_BRIDGE_SCRIPT")).new()
	bridge.name = "QAJevBridge"
	root.add_child(bridge)
