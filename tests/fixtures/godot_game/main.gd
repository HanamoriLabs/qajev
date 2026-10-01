extends Control
# A menu with Start and Quit game; Start shows "Playing", Escape then shows "Paused". A button on a CanvasLayer
# scaled 2x (as games scale modals for phones) shows "Scaled clicked": it must be clicked where it is drawn.

var mode := "menu"
var status: Label

func _ready() -> void:
	status = Label.new()
	status.text = "Main menu"
	status.position = Vector2(20, 20)
	add_child(status)
	_button("Start", Vector2(20, 80), _on_start)
	_button("Quit game", Vector2(20, 140), func() -> void: get_tree().quit())
	var layer := CanvasLayer.new()
	layer.scale = Vector2(2, 2)
	add_child(layer)
	_button("Scaled", Vector2(200, 150), func() -> void: status.text = "Scaled clicked", layer)

func _button(label: String, at: Vector2, action: Callable, parent: Node = self) -> void:
	var b := Button.new()
	b.text = label
	b.position = at
	b.size = Vector2(160, 40)
	b.pressed.connect(action)
	parent.add_child(b)

func _on_start() -> void:
	mode = "playing"
	status.text = "Playing"

func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and event.keycode == KEY_ESCAPE and mode == "playing":
		mode = "paused"
		status.text = "Paused"
