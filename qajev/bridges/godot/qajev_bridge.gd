extends Node
# QAJev's bridge inside a Godot game: JSON lines over 127.0.0.1:$QAJEV_BRIDGE_PORT.
#   {"op": "observe"}                      -> screen, texts, actions (label + position), state, fps, frame, paused
#   {"op": "act", "click": [x, y]}         -> a left click at the game's canvas coordinates
#   {"op": "act", "key": "Escape"}         -> a key press and release (Godot key names)
#   {"op": "act", "move": [x, y]}          -> the pointer moves there
#   {"op": "shot", "path": "/abs/x.png"}   -> the viewport saved as PNG
#   {"op": "quit"}
# Input goes into the game's own input system (Input.parse_input_event): it never moves the machine's real
# mouse or types on its keyboard. A per-game adapter ($QAJEV_ADAPTER, an object with observe(main, viewport_size) -> Dictionary)
# describes screens the game draws itself; Control nodes (buttons, labels) are found without one.

var _server := TCPServer.new()
var _peers: Array = []
var _adapter: Object = null
var _releases: Array = [] # [frames_left, InputEventKey]
var _window := Vector2i.ZERO # QAJEV_WINDOW=WxH: keep the game in a small window, even if it goes fullscreen
var _window_check := 0.0
var _pilot := false # the adapter's autopilot moves the pointer each frame (real-time play), Jev decides
var _touching := false # the pilot's finger is down (touch-steered games)
var _headless := false # no display: input is pushed straight into the viewport (Godot drops OS-style events)

func _ready() -> void:
	process_mode = Node.PROCESS_MODE_ALWAYS # keep answering while the game is paused
	var port := int(OS.get_environment("QAJEV_BRIDGE_PORT"))
	var err := _server.listen(port, "127.0.0.1")
	_headless = DisplayServer.get_name() == "headless"
	if _headless: # the headless window is 64x64; give the game its real canvas so layouts and hit tests hold
		get_window().size = Vector2i(ProjectSettings.get_setting("display/window/size/viewport_width"),
			ProjectSettings.get_setting("display/window/size/viewport_height"))
	var wanted := OS.get_environment("QAJEV_WINDOW").split("x")
	if wanted.size() == 2 and DisplayServer.get_name() != "headless":
		_window = Vector2i(int(wanted[0]), int(wanted[1]))
		_hold_window()
	var path := OS.get_environment("QAJEV_ADAPTER")
	if path != "":
		_adapter = load(path).new()
	print("QAJEV_BRIDGE ready port=%d err=%d adapter=%s" % [port, err, path.get_file()])

func _hold_window() -> void:
	# Games often open fullscreen on purpose (for players); a test run keeps them in a small window instead,
	# so the machine stays usable and frames stay cheap to render.
	var w := get_window()
	if w.mode != Window.MODE_WINDOWED or w.size != _window:
		w.mode = Window.MODE_WINDOWED
		w.size = _window
		w.position = Vector2i(40, 40)

func _process(delta: float) -> void:
	if _pilot and _adapter != null and _adapter.has_method("pilot") and _main() != null:
		_steer(_adapter.pilot(_main(), delta))
	if _window != Vector2i.ZERO:
		_window_check += delta
		if _window_check > 0.5:
			_window_check = 0.0
			_hold_window()
	for pending in _releases.duplicate():
		pending[0] -= 1
		if pending[0] <= 0:
			_send(pending[1])
			_releases.erase(pending)
	while _server.is_connection_available():
		_peers.append({"peer": _server.take_connection(), "buf": ""})
	for p in _peers.duplicate():
		var s: StreamPeerTCP = p["peer"]
		s.poll()
		if s.get_status() != StreamPeerTCP.STATUS_CONNECTED:
			_peers.erase(p)
			continue
		var n := s.get_available_bytes()
		if n <= 0:
			continue
		p["buf"] += s.get_utf8_string(n)
		while p["buf"].find("\n") >= 0:
			var i: int = p["buf"].find("\n")
			var line: String = p["buf"].substr(0, i)
			p["buf"] = p["buf"].substr(i + 1)
			var reply := _handle(JSON.parse_string(line))
			s.put_data((JSON.stringify(reply) + "\n").to_utf8_buffer())

func _handle(req) -> Dictionary:
	if typeof(req) != TYPE_DICTIONARY:
		return {"ok": false, "error": "not a JSON object"}
	match str(req.get("op", "")):
		"observe":
			return _observe()
		"act":
			return _act(req)
		"shot":
			return _shot(str(req.get("path", "")))
		"pilot":
			var was := _pilot
			_pilot = bool(req.get("on", false)) and _adapter != null and _adapter.has_method("pilot")
			if not _pilot:
				_lift()
				if was and _adapter.has_method("pilot_off") and _main() != null:
					_adapter.pilot_off(_main()) # hand the controls back (e.g. a racket the pilot moved directly)
			return {"ok": true, "pilot": _pilot}
		"quit":
			get_tree().quit(0)
			return {"ok": true}
	return {"ok": false, "error": "unknown op"}

func _main() -> Node:
	return get_tree().current_scene

func _observe() -> Dictionary:
	var vp := get_viewport().get_visible_rect().size
	var out := {"ok": true, "screen": "", "texts": [], "actions": [], "state": {},
		"fps": Engine.get_frames_per_second(), "frame": Engine.get_process_frames(),
		"paused": get_tree().paused, "size": [vp.x, vp.y], "pilot": _pilot,
		"perf": {"frame_ms": Performance.get_monitor(Performance.TIME_PROCESS) * 1000.0,
			"memory_mb": Performance.get_monitor(Performance.MEMORY_STATIC) / 1048576.0,
			"objects": Performance.get_monitor(Performance.OBJECT_COUNT),
			"nodes": Performance.get_monitor(Performance.OBJECT_NODE_COUNT)}}
	_walk(get_tree().root, out)
	if _adapter != null and _main() != null:
		var extra: Dictionary = _adapter.observe(_main(), vp)
		for key in extra:
			if key in ["texts", "actions"]:
				out[key] = extra[key] + out[key] # the game's own description first
			else:
				out[key] = extra[key]
	return out

func _walk(node: Node, out: Dictionary) -> void:
	# Visible Control nodes: buttons become click actions, labels become texts.
	if node is CanvasLayer and not node.visible:
		return
	if node is Control:
		var c: Control = node
		if not c.is_visible_in_tree():
			return
		# Where it is drawn: with its CanvasLayer's transform (games scale modals for phones), which
		# get_global_rect() leaves out. Canvas coordinates, like everything observe reports.
		var at := c.get_global_transform_with_canvas() * (c.size / 2.0)
		if c is BaseButton and not (c as BaseButton).disabled:
			var label := ""
			if "text" in c:
				label = str(c.get("text"))
			if label == "":
				label = c.tooltip_text if c.tooltip_text != "" else str(c.name)
			out["actions"].append({"id": "ctl:" + str(c.get_path()), "label": label, "kind": "click",
				"x": at.x, "y": at.y})
		elif c is Label or c is RichTextLabel:
			var t := str(c.get("text")).strip_edges()
			if t != "":
				out["texts"].append(t)
	for child in node.get_children():
		_walk(child, out)

func _act(req: Dictionary) -> Dictionary:
	var vp := get_viewport()
	if req.has("move") or req.has("click"):
		# Coordinates are the game's canvas coordinates (what observe reports). Events go through Godot's input
		# system as if from the OS, so GUI controls and _input/_unhandled_input all see them: convert canvas to
		# window coordinates with the window's own stretch transform.
		var p: Array = req.get("click", req.get("move"))
		var pos := Vector2(float(p[0]), float(p[1]))
		var at: Vector2 = pos if _headless else vp.get_final_transform() * pos
		var motion := InputEventMouseMotion.new()
		motion.position = at
		motion.global_position = at
		_send(motion)
		if req.has("click"):
			for pressed in [true, false]:
				var b := InputEventMouseButton.new()
				b.button_index = MOUSE_BUTTON_LEFT
				b.button_mask = MOUSE_BUTTON_MASK_LEFT if pressed else 0
				b.pressed = pressed
				b.position = at
				b.global_position = at
				_send(b)
		return {"ok": true}
	if req.has("key"):
		var code := OS.find_keycode_from_string(str(req["key"]))
		if code == KEY_NONE:
			return {"ok": false, "error": "unknown key " + str(req["key"])}
		var down := InputEventKey.new()
		down.keycode = code
		down.physical_keycode = code
		down.pressed = true
		_send(down)
		var up := down.duplicate()
		up.pressed = false
		_releases.append([2, up]) # released two frames later, as a real key tap
		return {"ok": true}
	return {"ok": false, "error": "act needs click, move or key"}

func _steer(plan) -> void:
	# The adapter's pilot says where the cursor should be: {"target", "cursor", "input": "mouse" | "touch"}.
	if plan == null:
		return
	var target: Vector2 = plan["target"]
	if str(plan.get("input", "mouse")) == "touch":
		var cursor: Vector2 = plan["cursor"]
		if not _touching: # one finger down on the playfield claims movement (a trackpad-like relative drag)
			var down := InputEventScreenTouch.new()
			down.index = 0
			down.pressed = true
			down.position = _to_window(cursor)
			_send(down)
			_touching = true
		var drag := InputEventScreenDrag.new()
		drag.index = 0
		drag.position = _to_window(target)
		drag.relative = target - cursor
		_send(drag)
	else:
		var motion := InputEventMouseMotion.new()
		motion.position = _to_window(target)
		motion.global_position = motion.position
		_send(motion)

func _lift() -> void:
	if _touching:
		var up := InputEventScreenTouch.new()
		up.index = 0
		up.pressed = false
		_send(up)
		_touching = false

func _to_window(at: Vector2) -> Vector2:
	return at if _headless else get_viewport().get_final_transform() * at

func _send(event: InputEvent) -> void:
	if _headless:
		get_viewport().push_input(event, true)
	else:
		Input.parse_input_event(event)

func _shot(path: String) -> Dictionary:
	var tex := get_viewport().get_texture()
	if tex == null:
		return {"ok": false, "error": "no rendered frame (headless)"}
	var img := tex.get_image()
	if img == null or img.is_empty():
		return {"ok": false, "error": "no rendered frame (headless)"}
	if img.get_width() > 1600: # reports stay light: a 4K frame is scaled down
		img.resize(1600, int(img.get_height() * 1600.0 / img.get_width()), Image.INTERPOLATE_BILINEAR)
	var err := img.save_jpg(path, 0.82) if path.ends_with(".jpg") else img.save_png(path)
	return {"ok": err == OK, "path": path, "w": img.get_width(), "h": img.get_height()}
