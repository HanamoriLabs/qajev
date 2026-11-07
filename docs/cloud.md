# QAJev Cloud: run your tests in the cloud

QAJev Cloud runs your QAJev tests on cloud machines, not on your computer. Your agents start the runs, and the
dashboard at [cloud.qajev.com](https://cloud.qajev.com) shows every run, its report and its screenshots. This page
says how to join a team, how to set up a computer or a cloud workstation, and what to tell your agents. The
[CLI reference](cli.md#qajev-cloud-where-a-run-goes) has every flag.

## What you need

- **A place on a team.** The team's owner adds your email to the team. Then you sign in at cloud.qajev.com with
  Google, GitHub, or an email and password. Only a verified email on the team list gets in.
- **The role `admin`**, to make your own keys. A `viewer` can see runs and reports but cannot start runs or make keys.
- **QAJev on the computer your agents use.** A Mac, or Linux (also a cloud workstation such as a Twinbox box).
- **No model key.** A cloud run uses QAJev Cloud's models and spends the team's credits. A model key on your computer
  is only for runs on that computer.

Everyone on a team sees every run, report and project of that team, and every run spends the team's credits.

**A run needs a project that the team runs in the cloud.** The cloud machines take runs only of the projects that
the team's owner has enabled for them. A run with no project, or with another one, is refused before it starts: "This
project does not run on our boxes yet". Send the owner your project's name and its repository, and wait for their
word before your first run.

## 1. Sign in and make a key

1. Open [cloud.qajev.com](https://cloud.qajev.com) and sign in with the email that the owner added.
2. Open **Settings**, then the keys section.
3. Make a key with the scopes **read** and **run**. Give it a name that says where it lives, for example
   `alex-workstation`. Every key expires, at most 365 days after you make it.
4. Copy the key. It shows only once. Never paste it in a chat, a commit or a suite.

You can delete a key in Settings at any time. Agents that use it stop at once.

## 2. Set up the computer

Install QAJev (see [Getting started](getting-started.md)):

```bash
curl -LsSf https://qajev.com/install.sh | sh
```

Then give QAJev the key. QAJev never prints it.

- **On a Mac:** keep it in Keychain, as the service `qajev-cloud-agent`. This command asks for the key, so it is not
  in your shell history:

  ```bash
  security add-generic-password -U -a qajev -s qajev-cloud-agent -w
  ```

- **On Linux, for example a Twinbox box:** put it in QAJev's own settings file, which only you can read. Type these
  commands in an interactive terminal on that computer (a desktop terminal, or `ssh -t`). The command asks for the
  key, so it is not in your shell history:

  ```bash
  mkdir -p ~/.qajev && touch ~/.qajev/.env && chmod 600 ~/.qajev/.env
  sed -i -E '/^[[:space:]]*(export[[:space:]]+)?QAJEV_CLOUD_KEY[[:space:]]*=/d' ~/.qajev/.env   # removes an old key
  printf 'QAJev Cloud key: '; IFS= read -rs k; echo
  [ -s ~/.qajev/.env ] && [ -n "$(tail -c1 ~/.qajev/.env)" ] && echo >> ~/.qajev/.env
  printf 'QAJEV_CLOUD_KEY=%s\n' "$k" >> ~/.qajev/.env; unset k
  ```

  QAJev reads the first `QAJEV_CLOUD_KEY` line, so the `sed` line removes an old key before the new one goes in. Run
  the same commands again when you replace the key. QAJev reads `~/.qajev/.env`, never a project's own `.env`.

  Never put the key in a command line: not in `box_run`, `agent_run` or a job, and not in an agent's chat. Other users
  of the computer can see a command line, and the agent's transcript keeps it.

Now send this computer's runs to the cloud, and check the set-up:

```bash
qajev mode cloud      # this computer's runs go to QAJev Cloud from now on
qajev doctor          # the "cloud" line says where the key was found, and that the dashboard answers
```

`qajev doctor` does not send the key. The first run proves the key: in the repository folder of a project the owner
enabled, `qajev smoke https://example.com/ --runner cloud` must show a run address. A refused key gives HTTP 401 or 403.

`qajev mode local` sends them back to this computer. `--runner cloud` on one command sends only that run.

## 3. Connect your agents

Add the MCP server to each agent (see [MCP](mcp.md)). For Claude Code:

```bash
claude mcp add -s user qajev -- qajev mcp
```

The MCP server reads the same `~/.qajev/.env` and Keychain, so it finds the key. Then paste the
[agent prompt](#the-agent-prompt) below into the agent's instructions (`CLAUDE.md`, `AGENTS.md`, or a system
prompt), after the general [agent prompt](../AGENT_PROMPT.md).

## 4. Run a test

Start in the project's repository folder, so QAJev knows the project (see [Projects](#what-reaches-the-cloud-and-what-does-not)):

```bash
cd ~/code/shop
qajev smoke https://staging.example.com/ --runner cloud
qajev check https://staging.example.com/pricing -g "Find the Pro price. Stop when it shows." -t '$29' --runner cloud
qajev run suites/shop.yaml --runner cloud
qajev run suites/local.yaml --runner cloud --serve dist:5173   # ships your built folder; the suite opens 127.0.0.1:5173
```

Each command prints the run's address on the dashboard at once, then its progress, the gate and the report's address.
The exit code is `0` only on PASS. `--background` returns the run id (`r_...`) at once:

```bash
qajev jobs r_abc123          # its state
qajev report r_abc123        # its report
qajev stop r_abc123          # stop it
qajev cloud-runs --hours 24  # the team's cloud runs of the last day
```

## What reaches the cloud, and what does not

- **A public site** (production read-only, or a staging site) works from any computer.
- **A site you build** goes with `--serve DIR[:PORT]`. The folder must be a clean build: QAJev refuses a folder with an
  env file, a key, credentials or a `.git` folder, and names them.
- **A dev server on your own `127.0.0.1`** reaches the cloud only through a tunnel from a computer that the team
  connected as its own. A plain run key cannot open a tunnel. Use `--serve`, or a staging site.
- **These stay on your computer**, and `--runner cloud` refuses them with the reason: a signed-in browser profile,
  games and native apps (`play`), real devices, `--cdp-url` and `--allow-commands`. See the
  [CLI reference](cli.md#qajev-cloud-where-a-run-goes) for the full list.
- **A cloud run never falls back to your computer.** When it cannot go, it stops and says why.
- **Projects.** A run goes to the cloud machines only with a project the owner enabled for them (see
  [What you need](#what-you-need)). QAJev finds a run's project in this order: `--project NAME` on `qajev run` and
  `qajev smoke`; the suite's own project; the repository folder the command starts in (its `.qajev/project.toml`);
  then the one project set up on this computer whose site is the URL's site. `--project` needs the project set up
  on this computer too. So start every command in the project's repository folder. `qajev check` has no
  `--project`: the folder names its project. The run carries that name even when the dashboard has no such
  project yet, in QAJev releases after 0.4.50 (0.4.50 and older sent a name only for a project the dashboard knew).

## Example: agents on a Twinbox box

A Twinbox box is a cloud Linux workstation with its own agents. Set it up once:

1. Open an interactive terminal on the box: its desktop terminal, or `ssh -t`. `box_run` has no terminal, so it cannot
   ask for the key.
2. Install QAJev, and add the key to `~/.qajev/.env` as in step 2.
3. Run `qajev mode cloud` and `qajev doctor`.
4. Add the MCP server to each agent on the box, and paste the agent prompt below into its `CLAUDE.md` or `AGENTS.md`.

The agents then start runs from the box, and QAJev Cloud's machines run them. The box's own browser stays free for
your work. A dev server on the box goes to the cloud with `--serve` after a build, or through a staging address.

## The agent prompt

Paste this block into the agent's instructions, after [AGENT_PROMPT.md](../AGENT_PROMPT.md). Edit the last lines for
your sites.

```markdown
## QAJev Cloud

Our QAJev tests run in QAJev Cloud, never on this computer. This computer's mode is `cloud` (`qajev mode`).

- Start every QAJev run with `runner: "cloud"` (MCP) or `--runner cloud` (CLI). Never pass `runner: "local"`.
- A cloud run returns its id (`r_...`), its `url` and state `queued` at once. That is not a pass. Wait with
  `qa_job(r_...)` (CLI: `qajev jobs r_...`) until it ends, then read its gate.
- Report each run to the person with its gate, its `url` and its `report_url`. A pass needs the gate PASS.
- Outcomes: `pass`; `fail` (the product is wrong); `stuck` (Jev found no way forward: check it by hand, often UX);
  `harness` (trouble in the test set-up, not the product: retry once, then report it); `unverified` (no checks).
- When a cloud run is refused or cannot start, report the reason. Do not rerun it on this computer.
- Stop only your own runs: `qa_stop(r_...)` (CLI: `qajev stop r_...`).
- `qajev cloud-runs --hours 24` (MCP `qa_cloud_runs`) lists the team's recent cloud runs.
- Test a public or staging address, or ship a clean build with `serve: "dist"` (CLI `--serve dist[:PORT]`). A dev
  server on this computer's 127.0.0.1 does not reach the cloud.
- Games, native apps and signed-in profiles cannot run in the cloud. Say so; do not run them here unless the person
  asks.
- Never print, copy or ask for the QAJev Cloud key. Never open `~/.qajev/.env` or the Keychain item. QAJev finds the
  key itself; `qajev doctor` says whether it is found.
- Start every QAJev run in the project's repository folder, so the cloud knows its project. `qa_project_run` takes
  the project by name (`project: "NAME"`). The cloud refuses a run of a project that is not enabled for it; report
  that refusal, and do not rerun it here.
- Every cloud run spends the team's credits. Run what the change needs: a `qa_smoke` first (no model calls), then the
  project's objectives or one `qa_check` with strict expectations.

Our projects: `shop` (enabled for the cloud). Our sites: <https://staging.example.com> (staging),
<https://example.com> (production, read-only).
```

## When something goes wrong

- **`no QAJev Cloud agent key`**: the key is not in `~/.qajev/.env` or Keychain. Check with `qajev doctor`.
- **HTTP 401 or 403**: the key expired, was deleted, or lacks the `run` scope. Make a new one in Settings, and put it
  in with the commands of [step 2](#2-set-up-the-computer). They remove the old key.
- **A run waits**: its progress line says why, in the dashboard's words: cloud machines are off, today's cloud minutes
  are used up, the month's model spend reached its limit, or the team has too few credits.
- **"This project does not run on our boxes yet"**: the owner has not enabled the project for the cloud machines.
  Send them its name.
- **"No project"**: QAJev found no project for the run, or the name has characters the dashboard does not take. In
  QAJev 0.4.50 and older, a project the dashboard did not know also showed this. See [Projects](#what-reaches-the-cloud-and-what-does-not).
