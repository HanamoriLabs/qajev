# The dashboard

`qajev dashboard` is one local web page with every QAJev run on this machine, any agent's: what is running now,
what is queued, and every finished run and project report. Open a run to see each test, what it proves, what went
wrong, its screenshots and the model's decisions. From the page you can stop a run, rerun it, or start a project's
objectives.

## Start it and stop it

```bash
qajev dashboard --background --open   # start it (it outlives the terminal) and open it in the browser
qajev dashboard --open                # already running: open it (prints the address too)
qajev dashboard                       # run it in this terminal; Ctrl-C stops it
qajev dashboard --stop                # stop the one running (it returns once the dashboard has exited and freed
                                      # its lock; after 15 s, an error)
```

| Option | Does |
|---|---|
| `--background` | start it detached: it keeps running after the terminal closes; prints its address and returns |
| `--open` | open the address in your browser |
| `--port PORT` | the first port to try on 127.0.0.1 (default 8790; it tries the next 19 if taken) |
| `--json` | print the address as JSON (`url`, `already_running`) |
| `--stop` | stop the dashboard running on this machine |
| `--new-key` | start with a new key: every address printed before stops working |

Only one dashboard runs on a machine. A second `qajev dashboard` does not start another: it prints the address of the
one running (`already running`).

## Where it runs, and its key

- It serves **127.0.0.1 only**, and answers only requests addressed to `127.0.0.1:PORT` or `localhost:PORT`.
- The address it prints carries a **key**: `http://127.0.0.1:8790/?k=...`. Opening it once turns the key into a
  cookie and takes it out of the address bar. Keep the address to yourself: anyone with it, on this machine, can
  see your runs and start or stop them.
- The key is kept in `~/.qajev/dashboard.key` (readable by you only, 0600), so a restart keeps the same address and
  an open tab keeps working. `qajev dashboard --new-key` makes a new one (stop the dashboard first).
- `~/.qajev/dashboard.json` (0600) records the running one: its process, port and key. It is removed when the
  dashboard stops.
- A background dashboard writes what it says to `~/.qajev/dashboard.log`.
- Actions (stop, rerun, start) need the key in a request header that only the page has, so another site open in your
  browser cannot trigger them. Report pages open sandboxed, and the page serves a run's own files only.

## What it shows

**The list of runs** (newest first, the latest 400): QAJev's jobs from `~/.qajev/jobs`, queued, running or finished,
and the project runs filed in a project's reports without a job behind them.

- Search, a chip per project (the QAJev project, else the site or the game), and filters for the result (live,
  PASS, FAIL, INCOMPLETE), the kind (games, checks, suites, smoke crawls) and the period. The filters stay in the
  address, so a view can be bookmarked.

**A run**: what it is, the command and folder, a bar of the results, its **UX** when it has any (how many UX notes
its tests have, by what they rest on, and the design consistency per device), the screenshots in a strip (click one
to enlarge; the arrows step through them), and its tests. "Not passed" shows only the tests to look at. Each test
has:

- **what it proves**: its `about`, under its name ([Writing tests](writing-tests.md) says how to give one);
- **for people**: "What went wrong" (or "What it proved"): each check as a plain sentence, failed ones first, with
  what was there instead (for a missing text, the closest text on the page), and the serious problems seen;
- **UX notes**, when it has any: their count beside its name (`3 UX notes`), and in the test each note by what it
  rests on, with its rule and examples. A UX note is advice: it never changes the test's result or the gate
  ([Reports](reports.md#ux));
- **for agents** (folded): the raw reason, checks and findings, the model's decisions (unsure ones marked; a move
  that went stale says why: what covered its target, or what changed), its actions and what the screen said, and
  **Copy for an agent**: the test, its checks and UX notes, the run folder, the command and
  `qajev rerun JOB --failed`, ready to paste into an agent's chat.

**A running run** updates as it happens: the page holds a stream to the dashboard, which pushes each step, test and
frame. The running test shows what it is doing now, its last steps, and its **screen, live**: a frame every 2 s for
website and Electron runs (Godot and mobile runs show their steps only). Frames are taken only while someone has the
run open, so an unwatched run costs nothing.

**Actions**: **Stop** a running job; **Rerun** a finished one, all of it or only its failed tests (as
`qajev rerun`); **New run** starts a project's stored objectives. It never reruns a job that ran shell commands
(`--allow-commands`): do that in a terminal.

## Open a run

- Click it in the list. The address then ends in `#run=<id>`; that address opens the same run again later.
- From a job id (`qajev jobs`, an agent's `qa_job`): open the dashboard and add `#run=<job id>` to its address.

## The MCP tools

The MCP tools do not put the dashboard's address in their results: it carries the key, and tool results end up in
agent transcripts. A run started from MCP is a QAJev job like any other, so it is in the dashboard's list. Its
result gives:

- `job`: the job id (open it as `#run=<job id>`; `qa_job` and `qa_jobs` show the same jobs);
- `report_html` and `report_md`: the run's report files;
- `about_missing`: the tests that do not say what they prove.

See [MCP](mcp.md).

## Troubleshooting

| You see | Why, and what to do |
|---|---|
| "Open the address `qajev dashboard` printed (it carries the key)" | The page has no key, or an old one (after `--new-key`). Run `qajev dashboard --open`. |
| "another dashboard holds the lock but is not serving" | A dashboard is starting or stopping. Wait a few seconds and run it again; `qajev dashboard --stop` stops one that is stuck. |
| "no free port from 8790 to 8809" | Those ports are taken. Pick another range: `qajev dashboard --background --port 9100`. |
| It printed a different port than last time | 8790 was taken, so it took the next free one. The address it prints is the one to use. |
| "the dashboard did not start" | The tail of `~/.qajev/dashboard.log` is in the message. |
| A run is not in the list | The list shows the latest 400 runs. A project run shows when its report is filed in the project's reports; a run whose job folder was removed shows only if it was a project run. Check the search box and the filters (they are in the address). |
| A running test shows no screen | Frames are for website and Electron runs, while the run is open. Godot and mobile runs show their steps only. |

The dashboard reads the same files as `qajev jobs` and [`qajev top`](jobs-and-top.md), the terminal view.
