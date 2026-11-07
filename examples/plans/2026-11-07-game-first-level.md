# Plan: a new player can start the game and finish level 1 (7 Nov 2026)

## Why

The game's lead, 7 Nov 16:30: "Before tonight's build goes to the testers, prove a new player gets from the title
screen through level 1, and that it stays smooth." Build 0.9.3 changes the level 1 map and the pause menu.

## Target

The game's Godot project folder (the one with `project.godot`) at build 0.9.3, commit 4f2a9c1, on this computer.
The game's QAJev adapter is `tools/qa/adapter.gd`. A fresh save profile: the run starts from a new game.

Not covered: levels 2 and up, controllers, and the online leaderboard (the build has it switched off).

## When

The runs start only when all of these hold. I check each one right before the run:
- The project folder is on commit 4f2a9c1 (`git rev-parse HEAD`), with no changes (`git status`).
- `qajev plan tools/qa/first-level.yaml` says every step says what it proves.
- No other game run is going on this computer (`qajev jobs`).
- The approver's line is the last line of this file.

## The runs

1. `qajev play . --adapter tools/qa/adapter.gd --suite tools/qa/first-level.yaml --json --plan tools/qa/plans/2026-11-07-game-first-level.md`

One run, with its steps in order.

## What each step proves

| Step | Claim: a pass means | Fault it catches |
|---|---|---|
| title screen | the game starts and shows its title menu within 20 s | a crash at start, a black screen, a slow load |
| new game | "New game" starts level 1 | a dead menu button, a missing first scene |
| pause menu | the pause menu opens and Resume returns to play | the 0.9.3 pause change breaking Resume |
| play level 1 | the game's autopilot plays level 1 to its end, and Jev makes each choice it stops for; its slowest 10% of frames stay at or above 50 fps (`min_fps: 50`) | a stuck path in the new map, a soft lock, a frame rate drop |
| level complete | the "Level complete" screen shows with a score | a level that never ends, a score of zero |

No planted fault: the steps check the game's own state through its adapter. The adapter's own tests cover a wrong
state.

## Expected result

Gate PASS. A FAIL is a real finding for the game's lead, with its frame, its step and the game's state.

## Cost and load

Jev for the choices and the menus, with a cap of $1.00. Clef looks at the title screen only, within the same cap.
This computer: one game window and its GPU, about 10 minutes.

## Data and accounts

A fresh save profile only, deleted after the run. No account. The game makes no network call in this build.

## The stop rule

Stop, change nothing, and tell the approver at once if:
- the run gives HARNESS or INCOMPLETE twice;
- the game crashes twice at the same step (the crash itself is a finding: I report it once, with its log);
- the project folder is not on commit 4f2a9c1.

A stopped run is not run again without a new approval line.

## The result

One message to the game's lead and the approver: the gate, the report_html path, the frame rate per step, and each
finding with its frame.
