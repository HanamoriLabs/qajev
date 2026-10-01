extends RefCounted
# QAJev adapter for Suho (a Godot game). Suho draws its front-end itself (TitleScreen in
# title.gd, not Control nodes), so this describes it: which screen is up, the menu's buttons and where they are,
# and the run's state. Read-only: it only reads the game's fields; input still goes through the game's own path.

func observe(main: Node, vp: Vector2) -> Dictionary:
	var screens := ["SPLASH", "MENU", "GAME"]
	var screen: String = screens[int(main.get("_screen"))]
	var out := {"screen": screen, "texts": [], "actions": [], "state": {}}
	var t = main.get("_title")
	if screen != "GAME" and t != null:
		if int(t.phase) != 0:
			screen = "MENU" # main's _screen stays SPLASH for the whole title screen; the title's phase says menu
			out["screen"] = screen
		if int(t.phase) == 0:
			out["texts"].append("Studio splash (a click skips it)")
			out["actions"].append({"id": "skip_splash", "label": "Skip the studio splash", "kind": "click",
				"x": vp.x / 2.0, "y": vp.y / 2.0}) # title.gd: a click in phase 0 skips the splash
		elif t.show_howto:
			out["screen"] = "HOW TO PLAY"
			out["actions"].append({"id": "close_overlay", "label": "Close How to play", "kind": "key", "key": "Escape"})
		elif t.show_board:
			out["screen"] = "LEADERBOARD"
			out["actions"].append({"id": "close_overlay", "label": "Close the leaderboard", "kind": "key",
				"key": "Escape"})
		elif t.show_shop:
			out["screen"] = "UPGRADES"
			out["actions"].append({"id": "close_overlay", "label": "Close Upgrades", "kind": "key", "key": "Escape"})
		else:
			out["texts"].append("SUHO main menu")
			var items: Array = t._menu_items()
			var ids: Array = t._menu_actions()
			var rects: Array = t.menu_rects(vp)
			for i in items.size():
				var r: Rect2 = rects[i]
				out["actions"].append({"id": "menu_" + str(ids[i]), "label": str(items[i]), "kind": "click",
					"x": r.get_center().x, "y": r.get_center().y})
	var sim = main.get("sim")
	if screen == "GAME" and sim != null:
		var state := {"score": sim.score, "kills": sim.kills, "level": sim.level, "core_hp": sim.core_hp,
			"core_max_hp": sim.core_max_hp, "tick": sim.tick, "game_over": sim.game_over,
			"draft_open": not sim.draft.is_empty(), "settings_open": bool(main.get("_paused")),
			"enemies": sim.count, "weapons": sim.weapon_levels.size(), "cursor": [sim.cursor_x, sim.cursor_y]}
		out["state"] = state
		if not sim.draft.is_empty() and not sim.game_over:
			out["screen"] = "LEVEL UP"
			out["decision"] = true # the game waits for the player's pick: a decision for Jev
			out["texts"].append("Level up! Pick one upgrade. Core HP %d of %d, level %d, %d enemies on the field." % [
				int(sim.core_hp), int(sim.core_max_hp), int(sim.level), int(sim.count)])
			var tree = load("res://tree.gd")
			var rects: Array = main._card_rects()
			for i in sim.draft.size():
				var off: Dictionary = sim.draft[i]
				var oid: String = off["id"]
				var to: int = int(off["to"])
				var is_evo: bool = tree.EVOLUTIONS.has(oid)
				var tier: int = int(off.get("rarity", 4 if is_evo else 0))
				var step := "EVOLVE" if is_evo else ("LEARN" if to == 1 else ("MAX" if to == tree.MAX_LEVEL else "LV %d" % to))
				var desc := String(tree.display(oid)["desc"]) if is_evo else String(tree.WEAPONS[oid]["levels"][to - 1]["desc"])
				var r: Rect2 = rects[i]
				out["actions"].append({"id": "card_%d" % i, "label": "%s (%s, %s): %s" % [
					String(tree.display(oid)["name"]), String(tree.RARITY[tier]["name"]).to_lower(), step, desc],
					"kind": "click", "x": r.get_center().x, "y": r.get_center().y})
			return out
		if sim.game_over:
			out["screen"] = "GAME OVER"
			out["texts"].append("Game over. Score %d, kills %d." % [int(sim.score), int(sim.kills)])
			out["actions"].append({"id": "to_menu", "label": "Go back to the main menu", "kind": "key", "key": "Escape"})
		elif bool(main.get("_paused")):
			out["screen"] = "SETTINGS"
		else:
			out["texts"].append("Playing. Core HP %s, level %d, score %d." % [str(sim.core_hp), int(sim.level),
				int(sim.score)])
			out["actions"].append({"id": "open_settings", "label": "Open the settings (pause)", "kind": "key",
				"key": "Escape"})
	return out


# The game's own balance-bot strategy (simulate.gd): steer toward the enemy nearest the core, at a mouse-like
# 680 px/s. The bridge moves the pointer there each frame while its pilot is on; Jev still makes the decisions.
var _pilot_pos := Vector2.ZERO

func pilot(main: Node, delta: float):
	var sim = main.get("sim")
	if sim == null or sim.game_over or not sim.draft.is_empty() or bool(main.get("_paused")):
		return null
	if _pilot_pos == Vector2.ZERO:
		_pilot_pos = Vector2(sim.cursor_x, sim.cursor_y)
	var best := -1
	var best_d := INF
	for i in sim.count:
		var d: float = pow(sim.ex[i] - sim.center_x, 2) + pow(sim.ey[i] - sim.center_y, 2)
		if d < best_d:
			best_d = d
			best = i
	if best >= 0:
		var to := Vector2(sim.ex[best], sim.ey[best]) - _pilot_pos
		if to.length() > 1.0:
			_pilot_pos += to.normalized() * minf(680.0 * delta, to.length())
	# Desktop Suho reads the OS mouse position directly (no event can move it); its mobile build steers with
	# relative touch drags, which the bridge can send. Run with SUHO_FORCE_MOBILE=1 for piloted play.
	var touch = main.get("_touch")
	if bool(main.get("_is_mobile")) and touch != null:
		return {"target": _pilot_pos, "cursor": touch.cursor(), "input": "touch"}
	return {"target": _pilot_pos, "cursor": Vector2(sim.cursor_x, sim.cursor_y), "input": "mouse"}
