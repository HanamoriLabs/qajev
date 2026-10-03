# Testing games

Besides websites, QAJev tests games: made with **Godot**, or web games shipped as **Electron** desktop apps (the
usual way to put a web game on Steam). The graphics engine (Metal, DirectX, Vulkan, WebGL) does not matter:
QAJev talks to the game itself, not to its pixels.

## How it works

- **Jev reads text, never pixels.** A small **bridge** inside the game describes each screen as text plus labelled
  actions ("PLAY", "Settings", "Pause the game (Esc)"), and the game's own state (score, level, health...). Jev picks
  among those actions, just as on a web page.
- **QAJev starts its own copy** of the game with a **throwaway save folder**, so a player's real saves (and cloud
  saves) are never touched. Nothing is written into the game's project. For Godot, `user://` (saves, settings,
  logs) is moved there by giving the game its own `HOME`, and QAJev's boot script refuses to start the game when
  `user://` is anywhere else. A game that keeps files outside `user://` can read the folder from `QAJEV_USER_DIR`.
- **Input goes into the game only**, never to your real mouse or keyboard.
- **Verdicts come from the game's state**: which screen is up, what it says, state values (`game_over=false`,
  `kills=">= 20"`), frame rate, memory, and script or engine errors.
- **Never offered to Jev**: quit, exit to desktop, buy, delete or reset saves or progress, plus anything the game's
  adapter hides (for example online play). A suite can hide more labels (`hide: [...]`), or let an exact label
  through when the test needs it (`allow: [QUIT]`, see [Quitting on purpose](#quitting-on-purpose)).

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

- a **goal** step: Jev works the menus, as above;
- a **play** step: the game is played in real time for a while. The adapter's **pilot** steers (usually the
  game's own bot or autopilot), and Jev makes every **decision** the game stops for (level-up cards, dialogue
  choices, which item to wear...). QAJev samples frames per second, frame time, memory and the game's state every
  half second; or
- an **idle** step (`idle: 90`): nobody touches the game for that many seconds, then the step's checks run. It
  needs no pilot, so it works on a release build, for example to leave the game running while something outside
  watches it (a `--game-arg=--log-net-log=...` network log). If the game crashes or closes meanwhile, the step
  stops with an S1 finding and the rest of the session is skipped;
- a **js** step (`js: "<expression>"`, Electron only): runs the expression in the game's page (a promise is
  awaited) and records its value as `js_result`, plus any page error in the next half second as `page_errors`. An
  error the script schedules (`setTimeout(() => { throw new Error('probe') }, 0)`) reaches the page uncaught, as a
  real one would, for proving an error reporter. Later steps then see that error: give them
  `expect: {no_errors: false}`; or
- a **crash_renderer** step (`crash_renderer: true`, Electron only): crashes the game's page renderer on purpose
  (CDP `Page.crash`) so the app's crash reporter writes and sends its report. It passes when the renderer is gone
  and the app runs on. After it, `idle` steps watch the app's process (there is no page left to read) and other
  steps are skipped.

Any step, and the suite itself, can say what it proves with `about:` ("the build's rules hold: no banned jutsu is
picked"). The report and the dashboard show it under the step's name, so a pass says what it means.

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
  - name: the game over screen stays put for a minute
    idle: 60
    expect: {screen: GAME OVER}
```

A play step **fails** on a **soft-lock** (the game stops advancing while nothing waits for the player), engine or
script errors, a frame rate whose slowest 10% is under `min_fps`, or memory growing more than
`max_memory_growth_mb`. A game that closes mid-play gets an S1 "game crashed or closed" finding. One still running that
stops answering QAJev is measured for 3 s: when its renderer is using most of a CPU core, the game froze (an endless
loop: S1 "game froze"); when it idles, the debugger link is the likely cause (S2 "game stopped answering"). The finding gives the number ("renderer
CPU 1.04 cores over 3 s") so a person can judge. Two known limits: a game frozen in a deadlock waits at about 0% CPU,
so it is filed S2; and a game busy at 80% of a core or more when the debugger link stalls could be filed S1. Either way the step is a harness stop: never a pass, even
when the frame rate and memory held until then. A game left paused
by the step before also stops the step as a harness problem, instead of "playing" a frozen screen. While a play step runs, [`qajev top`](jobs-and-top.md) shows the live numbers.

`until` is what the step sets out to see: it ends the step early when the game's state matches (a key the state does
not have, such as `screen`, is read from the game's look), and the step gets a check "reached game_over = true".
Not met by the time cap, or the game over first, that check fails, with each key's last value in the detail. A soak
that only wants to stop early, and passes on the cap too, says so:

```yaml
  - name: playing on, up to 5 minutes or until the core falls
    play: {seconds: 300, until: {game_over: true}, until_optional: true}
    expect: {min_fps: 30}
```

A play step with no `decide` asks no model. At each decision the game's own bot picks, when its adapter offers one
(I'M HIM!: the bot's choice under the build's rules), and the step's reason counts those picks; a game with no bot
pick gets the first offer and an S3 "decision not made by a model". When the model does not make a decision it was
asked for (it answered DONE or BLOCKED, or picked something not on screen), the game's bot picks, or else the first
offer, and QAJev files an S3 "decision not made by" the model, by name (Jev, Clef). For a run that must be the
model's own route, set `strict_decisions: true` on the play step: the first such decision then fails the step there,
nothing clicked, with the screen and the model's answer in the reason.

### Looking at the screen (Clef)

With Clef as the decision model (it reads images; see [Configuration](configuration.md#keys)), a step can use the
game's screenshot:

- **Vision is on by default for games**: with Clef deciding and a window to look at (Electron, a mobile app, a Godot
  game run without `--headless`), Clef sees the game's screen with every decision (goal steps, and a play step's
  `decide`), not only the adapter's labels: card art, icons, a map. With Jev, or a headless Godot game, decisions use
  the labels only, without complaint. `vision: false` on a step or on the suite, or `--no-vision`, turns it off;
  `vision: true` or `--vision` asks for it and refuses to run without Clef and a window. Websites stay text-only
  unless asked (`qajev check --vision`).
- `looks` in a step's `expect` (or `--expect-looks "..."`, repeatable): statements judged by Clef from the screenshot
  at the end of the step, each passing at a probability of 0.5 or more.

```yaml
steps:
  - name: the level-up cards can be read
    play: {seconds: 60, decide: Pick the upgrade that best helps you survive.}
    vision: true
    expect: {min_fps: 30, looks: ["The health bar is visible in the top left and is not covered by a menu"]}
```

Asked for (`vision: true`, `--vision`, or `looks`), both need the game's window: QAJev refuses them with
`--headless` (MCP: `headless=false`), since a headless Godot game draws nothing to look at. They also refuse to start
without Clef.

### Running some steps only

A long session need not run whole to check one part of it. `--only STEP` (repeatable; MCP `only`) runs the named
steps, in the suite's order, in one fresh launch:

```yaml
steps:
  - name: verify helpers
    js: "window.__game !== undefined"
    setup: true                  # always runs, also with --only
  - name: boss 1
    play: {seconds: 120}
  - name: boss 2
    play: {seconds: 120}
    depends_on: [boss 1]         # --only "boss 2" runs boss 1 first
```

`qajev play GAME --suite session.yaml --only "boss 2"` runs `verify helpers`, `boss 1` and `boss 2`. A step with no
name is `step N`, its place in the whole suite. To run only what failed last time: `qajev rerun JOB --failed`
([CLI](cli.md#qajev-rerun)).

### Quitting on purpose

QUIT is hidden from Jev, because quitting ends the session. To test a normal quit (say, that the game sends its
session-end event), the suite allows the exact label and expects the game to close:

```yaml
allow: [QUIT]                # exact label, as the game shows it
steps:
  - name: quit from the main menu
    goal: Get past the intro screens and offers to the main menu, then choose QUIT. Stop when the game has closed.
    expect: {closed: true}
```

Without a suite: `qajev play GAME --goal "... choose QUIT ..." --allow QUIT --expect-closed`, or from MCP
`qa_play(..., allow=["QUIT"], expect_closed=true)`.

`closed: true` passes only when the game's process exits by itself with code 0; a crash, or a game still running
at the end of the step, does not pass. Steps after a quit are skipped, until a `relaunch` step (below).

### Saves: starting from one, and Continue (Godot)

Every launch gets its own empty throwaway save folder. Two suite keys work with saves inside it:

```yaml
seed: qa/fixtures/legacy-save    # copied into user:// before the first launch reads it
steps:
  - name: an old save still loads
    goal: Choose Continue. Stop when you are playing.
    expect: {screen: GAME}
  - name: the run is saved
    goal: Save from the pause menu. Stop when it says saved.
  - name: Continue after a restart
    relaunch: true               # quit the game and start it again on the same saves
    goal: Choose Continue. Stop when you are playing.
    expect: {screen: GAME}
```

`seed` is a folder inside the game's own `qa/` folder (anything else, symlinks leading out of it included, is
refused); its files are copied into `user://` once, on the first launch, after QAJev has checked that `user://` is
the throwaway folder. A `relaunch` step restarts the game on the same throwaway folder, then runs its own goal and
checks, if it has any; it also brings back a game an earlier step closed.

## Adapters

Games built from standard UI controls (Godot `Button`s and `Label`s, HTML buttons) work without an adapter: the
bridge finds them. An **adapter** is a small script that describes what a game draws itself, reads its state, and
optionally pilots it. Three real ones ship with QAJev as examples:

| Adapter | Game | Shows how to |
|---|---|---|
| `qajev/bridges/godot/adapters/suho.gd` | a Godot horde-survival game | describe a menu the game draws itself; level-up decisions; pilot with touch input |
| `qajev/bridges/godot/adapters/hypervolley.gd` | a Godot racket game | read match state; pilot with the game's own autopilot; hide online screens |
| `qajev/bridges/web/adapters/imhim.js` | an Electron brawler | screens from DOM overlays; keys; label settings options by their row, including rows scrolled out of view (the adapter scrolls to them); count short-lived aids across looks (captions, edge markers, menus read aloud) and report saved settings; in a dev build, open the dev menu and offer its options and buttons ("Boss: Fight"); hand every decision the game's bot waits on to Jev, or, with no `decide`, to the bot's own pick |

Two lessons from these adapters:

- **Name an action for where it leads.** Jev picks by meaning: "Go to the main menu (press Enter)" gets chosen
  for a goal in the menus, while "Press Enter to start" next to the same goal reads as no way forward.
- **Give same-text buttons their context.** The generic look keeps only the first of identical labels, so a
  settings screen with several ON/OFF rows offers one "OFF". The I'M HIM! adapter turns them into
  "Vibration: OFF" and marks the selected one "(current)", which an `expect: {text: ...}` can check.

When the game opens on a title screen, logos or a first-launch offer, say so in the goal ("Get past the intro
screens and offers to the main menu, then open SETTINGS ..."). A goal that starts "From the main menu" can leave
Jev answering BLOCKED on a pop-up it was not told to expect.

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
