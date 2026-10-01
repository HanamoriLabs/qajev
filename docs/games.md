# Testing games

Besides websites, QAJev tests games: made with **Godot**, or web games shipped as **Electron** desktop apps (the
usual way to put a web game on Steam). The graphics engine (Metal, DirectX, Vulkan, WebGL) does not matter:
QAJev talks to the game itself, not to its pixels.

## How it works

- **Jev reads text, never pixels.** A small **bridge** inside the game describes each screen as text plus labelled
  actions ("PLAY", "Settings", "Pause the game (Esc)"), and the game's own state (score, level, health...). Jev picks
  among those actions, just as on a web page.
- **QAJev starts its own copy** of the game with a **throwaway save folder**, so a player's real saves (and cloud
  saves) are never touched. Nothing is written into the game's project.
- **Input goes into the game only**, never to your real mouse or keyboard.
- **Verdicts come from the game's state**: which screen is up, what it says, state values (`game_over=false`,
  `kills=">= 20"`), frame rate, memory, and script or engine errors.
- **Never offered to Jev**: quit, exit to desktop, buy, delete or reset saves or progress, plus anything the game's
  adapter hides (for example online play).

## One check

```bash
# A Godot project folder (the one with project.godot)
qajev play path/to/my-godot-game \
  --goal "Start a new game, then pause it. Stop when the pause menu is open." \
  --expect-screen PAUSED

# Invisible and fastest (no window, no screenshot)
qajev play path/to/my-godot-game --headless --goal "..." --expect-text "Playing"

# An Electron game: the .app, or the Electron project folder
qajev play MyGame.app --adapter my-game.js --goal "Open the settings. Stop when they are open." --expect-screen SETTINGS
```

## A whole session: menus and real-time play

A **suite** plays one game session in steps, in order. Each step is either:

- a **goal** step: Jev works the menus, as above; or
- a **play** step: the game is played in real time for a while. The adapter's **pilot** steers (usually the
  game's own bot or autopilot), and Jev makes every **decision** the game stops for (level-up cards, dialogue
  choices, which item to wear...). QAJev samples frames per second, frame time, memory and the game's state every
  half second.

```yaml
# my-game.yaml;  run: qajev play path/to/my-game --suite my-game.yaml
name: my game
adapter: my-game            # a bundled adapter name, or a path to one
headless: false             # true: invisible, no screenshots
env: {MY_GAME_TEST_MODE: "1"}   # environment settings for the game (also --game-env KEY=VALUE)
steps:
  - name: the title gives way to the menu
    goal: Get past the splash screen. Stop when the main menu is visible.
    expect: {screen: MENU}
  - name: a player starts a run
    goal: Start a new game. Stop when you are playing.
    expect: {screen: GAME, state: {game_over: false}}
  - name: 90 seconds of play, Jev picks the upgrades
    play:
      seconds: 90
      decide: Pick the upgrade that best helps you survive.   # Jev's goal at each decision
    expect: {state: {kills: ">= 30"}, min_fps: 30, max_memory_growth_mb: 80}
  - name: the run pauses
    goal: Pause the game. Stop when the pause menu is open.
    expect: {screen: PAUSED}
  - name: playing on until the game ends (up to 5 minutes)
    play: {seconds: 300, until: {game_over: true}}
    expect: {min_fps: 30}
```

A play step **fails** on a **soft-lock** (the game stops advancing while nothing waits for the player), a crash,
engine or script errors, a frame rate whose slowest 10% is under `min_fps`, or memory growing more than
`max_memory_growth_mb`. A game left paused by the step before stops the step as a harness problem, instead of
"playing" a frozen screen. While a play step runs, [`qajev top`](jobs-and-top.md) shows the live numbers.

## Adapters

Games built from standard UI controls (Godot `Button`s and `Label`s, HTML buttons) work without an adapter: the
bridge finds them. An **adapter** is a small script that describes what a game draws itself, reads its state, and
optionally pilots it. Three real ones ship with QAJev as examples:

| Adapter | Game | Shows how to |
|---|---|---|
| `qajev/bridges/godot/adapters/suho.gd` | a Godot horde-survival game | describe a menu the game draws itself; level-up decisions; pilot with touch input |
| `qajev/bridges/godot/adapters/hypervolley.gd` | a Godot racket game | read match state; pilot with the game's own autopilot; hide online screens |
| `qajev/bridges/web/adapters/imhim.js` | an Electron brawler | screens from DOM overlays; keys; hand every decision the game's bot waits on to Jev |

### A Godot adapter

A GDScript `RefCounted` with:

```gdscript
func observe(main: Node, viewport_size: Vector2) -> Dictionary:
    return {
        "screen": "MENU",                        # what the checks compare
        "texts": ["Main menu"],                  # what Jev reads
        "actions": [                             # what Jev may do
            {"id": "play", "label": "PLAY", "kind": "click", "x": 640, "y": 360},
            {"id": "pause", "label": "Pause the game (Esc)", "kind": "key", "key": "Escape"},
        ],
        "state": {"score": 0, "game_over": false},   # values the checks read
        "decision": false,                       # true while the game waits for the player's choice
        "hide": ["PLAY ONLINE"],                 # labels Jev must never be offered
    }

# Optional, for real-time play: return {target, cursor, input: "mouse" | "touch"} to steer,
# or null when the pilot moved the player itself (then add pilot_off to hand the controls back).
func pilot(main: Node, delta: float):
    return null
```

`main` is the game's current scene. Coordinates are in the game's canvas; QAJev converts them.

### An Electron (web) adapter

A script that sets `window.__qajevAdapter` in the page:

```js
window.__qajevAdapter = {
  observe(base) {          // base: the visible buttons, text, fps and memory QAJev already found
    const g = window.game;
    return { screen: g.paused ? 'PAUSED' : 'GAME', state: { score: g.score },
             actions: [{ id: 'pause', label: 'Pause the game (Esc)', kind: 'key', key: 'Escape' }] };
  },
  pilot(on) { return false; },     // optional: start or stop the game's own bot; true if it has one
  act(action) { return { ok: false } },   // optional: actions of kind "adapter", carried out in the page
};
```

Electron apps get switches with `--game-arg` (or `args:` in a suite), e.g. `--game-arg=--query=autoplay=1`.

## Tips

- Give the game a **test hook** if you can: one function that returns the state as plain values (screen,
  health, score, open menus). Adapters then stay tiny, and checks read facts instead of guessing.
- A game's own **bot or autopilot** makes the best pilot: Jev is quick at decisions but is not built for twitch
  gameplay.
- Prefer **headless** for games that grab the mouse or pause when their window loses focus.
- Games that read the operating system's mouse position directly cannot be steered by injected input; use their
  touch or controller mode for piloted play.

## Not yet

Unreal and Unity bridges; mobile builds; a fallback that reads any window from its pixels.
