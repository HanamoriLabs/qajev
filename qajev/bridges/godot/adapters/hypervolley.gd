extends RefCounted
# QAJev adapter for HYPERVOLLEY (a Godot 4.7 game; internal name "racket"). Its menus are real
# Buttons (the bridge finds them); this names the screen from the current scene, reads the match state off main.gd,
# hides the screens that talk to the game's server, and pilots the player's racket with the game's own autopilot
# (main.gd _drive_autopilot, what its soak runs use). Read-only apart from the pilot, which drives the racket the
# way RACKET_AUTOPILOT=1 does. Surveyed 2026-10-01 at b7b2c29.

const STATES := ["SERVE", "RALLY", "SCORED", "GAME OVER"]
# PLAY ONLINE opens WebSockets, RANKED/TOURNAMENTS/LEADERBOARD call the server (net/): never in a QA run.
const HIDE := ["PLAY ONLINE", "RANKED", "TOURNAMENTS", "LEADERBOARD"]

var _last_ball := Vector3.INF
var _ticks := 0 # counts frames where the ball moved: a soft-lock is a ball that stops with nobody to serve

func observe(main: Node, vp: Vector2) -> Dictionary:
	var scene := String(main.scene_file_path).get_file().get_basename()
	var out := {"screen": scene.to_upper(), "texts": [], "actions": [], "state": {}, "hide": HIDE}
	var key := func(id: String, label: String, k: String) -> Dictionary:
		return {"id": id, "label": label, "kind": "key", "key": k}
	match scene:
		"splash":
			out["screen"] = "SPLASH"
			out["actions"].append({"id": "skip_splash", "label": "Skip the splash", "kind": "click",
				"x": vp.x / 2.0, "y": vp.y / 2.0}) # splash.gd: any key, click or touch skips
		"title":
			out["screen"] = "TITLE"
		"howto":
			out["screen"] = "HOW TO PLAY"
			out["actions"].append(key.call("leave", "Leave How to play (Esc)", "Escape"))
		"settings":
			out["screen"] = "SETTINGS"
			out["actions"].append(key.call("leave", "Leave the settings (Esc)", "Escape"))
		"garage":
			out["screen"] = "RACKETS"
			out["actions"].append(key.call("leave", "Leave the rackets (Esc)", "Escape"))
		"main":
			_match(main, out, key)
	return out

func _match(main: Node, out: Dictionary, key: Callable) -> void:
	var st: int = int(main.get("_state"))
	var score: Dictionary = main.get("_score")
	var goals: Dictionary = main.get("_goals")
	var menu = main.get("_menu")
	var results = main.get("_results")
	var ball = main.get("_ball")
	var paused: bool = main.get_tree().paused or (menu != null and menu.visible)
	if ball != null and ball.global_position != _last_ball:
		_last_ball = ball.global_position
		_ticks += 1
	var state := {"phase": STATES[st] if st < STATES.size() else str(st), "score_me": score.get(1, 0),
		"score_ai": score.get(2, 0), "goals_me": goals.get(1, 0), "goals_ai": goals.get(2, 0),
		"time_left": snappedf(float(main.get("_time_left")), 0.1), "sudden_death": bool(main.get("_sudden_death")),
		"serving": "me" if main.get("_server") == main.get("_player") else "ai",
		"game_over": st == 3, "paused": paused, "tick": _ticks}
	if st == 3:
		state["winner"] = "me" if score.get(1, 0) > score.get(2, 0) else "ai"
	if paused:
		state["settings_open"] = true # waits for the player: real-time play must not run on
	out["state"] = state
	if results != null:
		out["screen"] = "RESULTS"
	elif paused:
		out["screen"] = "PAUSED"
		out["actions"].append(key.call("resume", "Resume the match (Esc)", "Escape"))
	else:
		out["screen"] = "MATCH"
		out["actions"].append(key.call("pause", "Pause the match (Esc)", "Escape"))
	out["texts"].append("Match %s. You %d (%d goals) - AI %d (%d goals). %.0f s left%s." % [state["phase"],
		state["score_me"], state["goals_me"], state["score_ai"], state["goals_ai"], state["time_left"],
		", sudden death" if state["sudden_death"] else ""])

# Real-time play: the game's own autopilot moves, serves and punches for the player (main.gd _drive_autopilot).
func pilot(main: Node, delta: float):
	if not main.has_method("_drive_autopilot") or main.get_tree().paused or int(main.get("_state")) == 3:
		return null
	var player = main.get("_player")
	if player == null:
		return null
	player.auto_controlled = true # real input off while the autopilot has the racket
	main._drive_autopilot(delta)
	return null # it moved the racket itself: no pointer input for the bridge to send

func pilot_off(main: Node) -> void:
	var player = main.get("_player") if main.has_method("_drive_autopilot") else null
	if player != null:
		player.auto_controlled = false
