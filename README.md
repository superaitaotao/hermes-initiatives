# hermes-initiatives

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that adds a planning and decision layer on top of
Hermes Kanban. Kanban cards say *what to build next*; initiatives say *why*, which bets are open, how each one will be
measured, and which decisions only you can make.

It has three layers, all optional beyond the first:

| Layer | What it does |
|---|---|
| **Initiatives** | Markdown initiatives and cycles (one bet each: question, hypothesis, measurement contract, verdict), an ideas inbox, tasks and bugs, observations, card links, validation. Desktop page and a phone-friendly dashboard tab. |
| **Decision queue** | One Review queue for every decision that needs you: Kanban human gates, cycle gates, idea triage, and asks other jobs file. Answer in the UI, with Telegram buttons, or by replying `D3 A` in a topic. Agents settle what you delegated, learn rules from items you hand them (`auto`), sweep stale items, and you can overrule any of it. |
| **Autonomy** | Zero-token cron wake gates for a Kanban **recovery** job and the **review** job, human-gate detection, an optional merge-gate filer, prompt templates, and the bundled `kanban-sdlc-orchestration` / `kanban-human-gates` skills. `hermes initiatives setup` creates the jobs. |

Data stays in plain Markdown in a folder you choose (an Obsidian vault works well). Review state lives in SQLite under
`~/.hermes/plugin-data/initiatives/`.

## Install

Requires Hermes Agent `>= 0.23.0`.

```bash
hermes plugins install superaitaotao/hermes-initiatives --enable
systemctl --user restart hermes-dashboard   # or restart `hermes dashboard` however you run it
```

In the Desktop app, turn on the plugin's Desktop half in **Settings → Plugins**; the page is at `/initiatives`. On the
web dashboard the tab is **Initiatives** at `<dashboard URL>/initiatives`.

## Quick start

1. Copy the example root and settings:

   ```bash
   cp -r examples/initiatives ~/initiatives
   mkdir -p ~/.hermes/plugin-data/initiatives
   cp examples/settings.example.json ~/.hermes/plugin-data/initiatives/settings.json
   ```

   Edit `settings.json`: set `root` to the folder you copied (absolute path) and `kanban_board` to your board slug.
2. Check it: `hermes initiatives doctor --project my-project`, then
   `python skills/initiatives/scripts/validate_initiatives.py --project my-project` (with Hermes' Python).
3. Open the Initiatives page. Agents can now record observations, propose ideas and file tasks through the bundled
   `initiatives:initiatives` skill.
4. Optional, the autonomy layer: preview, then create the cron jobs in a profile.

   ```bash
   hermes initiatives setup --profile default --project my-project --dry-run
   hermes initiatives setup --profile default --project my-project
   hermes initiatives status --profile default
   ```

   See [autonomy/README.md](autonomy/README.md) for what each job does, the wrappers it writes, and how to remove them.

## Configure

`~/.hermes/plugin-data/initiatives/settings.json` (`HERMES_INITIATIVES_DATA_DIR` moves the whole data directory).
Every field except `projects[].id`, `label` and `root` is optional. Full example:
[examples/settings.example.json](examples/settings.example.json).

| Field | Meaning |
|---|---|
| `projects[].id`, `label`, `root` | Project key, display name, and the absolute initiative root folder. |
| `projects[].kanban_board` | Hermes Kanban board slug the project's cards live on. |
| `projects[].cwd` | Working directory for sessions and cron jobs (default: `root`). |
| `projects[].vault_root`, `vault_name` | Obsidian vault containing the root, for document links. |
| `projects[].intake_assignee`, `plan_assignee`, `plan_skills` | Lanes that admit new work and write plans; skills the plan lane loads. |
| `projects[].policy_skill` | Your project policy skill (lanes, risk rubric, merge rules). Review and recovery jobs load it. |
| `projects[].worker_profiles` | Kanban lane profiles; their comments count as worker progress, not answers. |
| `projects[].github_repo` | `owner/repo` whose PRs and main CI the recovery gate reads (unset: no GitHub calls). |
| `projects[].merge_reviewer` | Reviewer lane that parks passed reviews on a user merge approval; enables the merge-gate filer. |
| `review_target` | `telegram:<chat>:<topic>` where decisions are pushed and answers are read. Unset: UI only. |
| `review_approvers` | Telegram user ids allowed to answer (buttons or typed replies). Default: the chat's own id when `review_target` is a private chat; a group target needs this list, or every answer is refused. |
| `owner_name` | How agent-written text names the decision owner (default `the user`). |
| `timezone` | IANA zone for the push window and digest (default: host local time). |
| `push_start_hour`, `push_end_hour` | Push window, `[start, end)` hours (default 8–24). Decisions outside it wait. |
| `digest_at` | Local `HH:MM` when the daily digest becomes due (default `08:30`). |
| `alert_jobs` | `[{profile, job, label}]` cron jobs whose delivered output is scanned for asks that never became a review item. |

## Surfaces and behaviour

- **Data:** Markdown files in an initiative root. Each root holds `in-*/initiative.md`, `cycles/*.md`, `observations/*.jsonl`, `ideas/*.md`, and `tasks/*.md` (plus legacy `bugs/*.md`).
- **Backend** (`dashboard/plugin_api.py`, `/api/plugins/initiatives/*`): it reads files and Kanban (`mode=ro`). It writes user-requested task statuses/card links and new task notes, plus Review state, launches, summaries, and session defaults in the plugin data directory. Kanban card creation is explicit and uses the Kanban CLI.
- **Desktop page** (`desktop/plugin.js`, route `/initiatives`): it shows initiatives, cycles, cards with PRs, observations, the ideas inbox, Tasks, Bugs, Kanban Metrics, and sessions. It also starts sessions in a profile you pick on the window's active gateway (`host.state.connectionId`; profiles on other connections, such as this device's local runtime, are not offered): **Discuss progress** for the whole initiative, **Start session on this cycle** or **on this card** for one subject, and **Start triage** for the inbox. A card or cycle session gets only that subject's context.
- **Web dashboard page** (`dashboard/dist/index.js` + `style.css`, tab **Initiatives** at `/initiatives` on `hermes dashboard`): the phone surface. It is built for a 390 px screen and also works on desktop browsers. **Reviews beneath each initiative** answer decisions the same way the Desktop page does: pick an option, add an optional note, then Apply, Confirm for irreversible options, Defer until it changes, or Let agents handle it. You can take an auto item back and preview cited documents read-only. Waiting on you is always shown; In progress (opened when a confirmation waits), Handled by agents, Waiting for options, Watching, Deferred, Decided recently and Settled by agents are folds. **Initiatives** lists initiatives with status summaries, then each initiative's cycles, Kanban cards with PR links and latest observations, read-only. **Tasks** and **Bugs** are two tabs over the same task records, each filtered by status; each files a new record of its type and moves one through triaged, done (needs a card or a reason), cancelled (needs a reason) and reopened. It can also create the Kanban card for a task (blank assignee: the project's intake assignee), the same as on Desktop. It uses the same `/api/plugins/initiatives` backend through the dashboard's own login (`SDK.fetchJSON`) and writes only the five Review actions and those task routes. A read-only **Metrics** tab shows the same Kanban health as Desktop. Sessions, the Inbox, overrules and learned-rule switches stay on Desktop. From an iPhone, open `<dashboard URL>/initiatives` in Safari (for example over Tailscale) and use Add to Home Screen. A dashboard restart is needed after install or update.
- **Session defaults** (Sessions tab): one host-wide profile, model and reasoning effort for every session the page starts. With a **Profile** set, every Start button (Discuss, cycle, card, triage, attend, blocker) opens the session in that profile on the window's connection with no picker; **Ask each time** keeps the picker, and a default profile the connection lacks falls back to it with a warning. Stored in `<plugin data>/session-defaults.json` and sent as per-session `session.create` overrides. **Profile default** leaves the profile's own setting; profile config is never changed.
- **Skill** (`skills/initiatives`): any agent can call `record_observation.py`, `propose_idea.py`, `report_task.py` (and legacy `report_bug.py`), `link_cards.py`, and `validate_initiatives.py`. Agents never write user decisions. `link_cards.py` only appends to a cycle's `kanban_cards`; the user removes cards and sets `excluded_cards`.
- **Tasks and Bugs** (two tabs): one record store, two lists. A bug is `type: bug`, not a separate workflow: both tabs read the same canonical task API, share statuses and actions, and only scope the list, its count, its remembered status filter and the form to their type. **New task** takes title, description, and priority (`high`, `medium`, `low`); **Report bug** takes title and description (priority defaults to high) plus optional expected/repro/where. Agents can run `report_task.py --title "…" --description "…" --type task --priority medium` (or `--type bug`). New records live in `<root>/tasks/task-YYYYMMDD-<slug>.md`; legacy `bugs/bug-*.md` notes show under Bugs. Statuses are `open`, `triaged`, `in_progress`, `done`, and `cancelled`; Active is the default. Each tab count is the active records of its type. Expand a row for its description, evidence, notes, source, linked card and PR; bug-specific expected/repro/where fields appear only on bugs. **Create card** creates a Kanban card for the given assignee (blank: project `intake_assignee`), links `card_id`, and marks `in_progress`; retries cannot create a second card after a successful link. The user chooses triage, completion, cancellation, reopening, and card creation. Cancellation requires a reason; completion requires a linked card or a reason. **Start session on this task/bug** discusses context without changing files or cards until told; each tab lists the sessions about its own records (legacy `bug` sessions under Bugs).
- **Task API** (under `/api/plugins/initiatives`): `GET /projects/<id>/tasks` returns `tasks`, `statuses`, `open_statuses`, `types`, and `kanban`; optional `?type=task|bug` narrows the type. `POST /projects/<id>/tasks` takes `{title, description, type, priority}` plus optional bug/evidence fields and returns `{task}`. `PATCH /projects/<id>/tasks/<task>` takes `{status, reason}`; `POST /projects/<id>/tasks/<task>/card` takes `{assignee, note}`; `GET /projects/<id>/tasks/<task>/session-context` supplies a `task` session. The Desktop uses these canonical task endpoints for both types, including legacy bug IDs.
- **Backward compatibility**: existing `<root>/bugs/bug-*.md` notes and IDs remain in place and appear as `type: bug`; `fixing` → `in_progress`, `fixed` → `done`, `wontfix` → `cancelled`, `severity` → `priority`, and `fix_card` → `card_id` are mapped in the task view. Status changes through the task API update their legacy fields without migration. `report_bug.py` and `/bugs` endpoints remain available. Existing `kind: bug` sessions stay visible and retryable beside new `kind: task` sessions; retry preserves their original bug context and diagnostic prompt.
- **Metrics** (a tab on Desktop and web, `GET /projects/<id>/metrics?days=7|14|30|90[&bucket=day|week]`): Kanban health for the project's board, computed on request from the board database (`mode=ro`) and each lane's `state.db` Kanban sessions. KPIs (delivered and with-PR, lead time p50/p90, failed-run share and hours, first-pass review rate, runs per delivery, fresh tokens per delivery) are compared with the preceding equal window; charts show per-day (weekly for 90 days) delivery vs intake, failed runs by cause (rate limited, protocol violation, crashed, timed out, gave up, reclaimed, spawn failed), review rounds and fresh tokens. Buckets are local-time with explicit `start`/`end`; the newest is partial. **Right now** lists open/blocked/needs-you counts and the oldest blocked cards with their age. The board keeps only current state, so each request stores at most one hourly snapshot in `<plugin data>/metrics.db`; the board-state chart fills from those over time. Method notes ride in the payload and the page's "How these are counted" fold.
- **Card links**: a card whose body has the line `- Initiative cycle: <cycle-id>` shows on that cycle ("from board", flag `cards_unlinked`) until it is linked, and `daily_review.py collect` links it first thing.
- **Status summaries**: each active initiative card shows a short agent-written status (`GET /projects/<id>/summaries`, stored in `<plugin data>/summaries.db`, never in the vault). The 30-minute tick runs `status_summaries.py tick` (facts from code: cycles, cards, newest card comment, Review counts, hashed), writes prose only for initiatives whose facts changed, and `write` (all or nothing, 700 characters, hash must match the tick input). Unchanged initiatives cost no model call and are only re-confirmed. The card says "updated N min ago" and turns red when no tick has confirmed it for 70 minutes. Tick step: `skills/initiatives/references/review_tick.md`.
- **Daily review** (under each initiative, `daily_review.py`, `request_decision.py`): one queue for every decision that needs the user, across Kanban gates, cycle gates, idea triage, and asks that other jobs file with `request_decision.py`. It runs in its own 30-minute review cron (created by `hermes initiatives setup`, at :15/:45 by default; the Kanban recovery cron runs at :00/:30). Its wake gate (`autonomy/initiatives_review_wake_gate.py`) first runs `autonomy/kanban_human_gates_watch.py --gates-json`, which lists the cards that still need a human, and only listed cards become new Kanban items. An existing item that leaves the list stays active until a fresh source read proves done, archived, or a missing card. Unfinished sources keep their short IDs and refreshed context; list omission is not completion. Each tick writes options only for new or changed items (`daily_review.py tick` / `write`), pushes each new decision once to the configured Telegram topic (`notify`, inside the push window `push_start_hour`–`push_end_hour`; `settings.json` → `review_target`), and from `digest_at` sends the daily digest by initiative (`daily`, `send`; retried until delivered). Tick steps: `skills/initiatives/references/review_tick.md`. Answer beneath the initiative in the Initiatives tab, or tap a pushed decision's inline buttons (one per option, plus `Defer` and `Agents handle it`; sent through the Bot API because `hermes send` carries no keyboard, and a tap and a typed line run the same executor, with the pushed message rewritten with the outcome), or reply in that topic with only answer lines (`D3 A`, `D3 B: note`, `confirm D3`, `defer D3 until 2026-10-20`); the `post_gateway_admission` hook takes those messages before the agent and passes any other text through. The executor (`hermes_initiatives_core/review_exec.py`) rechecks the source, applies the option (Kanban through the `hermes kanban` CLI; cycle and idea files through locked, read-back edits of user-owned fields), copies the note unchanged, and logs every step. Irreversible options wait for `confirm`. **Auto**: the user can hand one item to the agents ("Let agents handle it" beneath the initiative, or `auto D3: note` in the topic; `manual D3` takes it back). The next review tick applies one of its reversible options, resolves it, or returns it (`daily_review.py auto`, `hermes_initiatives_core/review_auto.py`); an applied option is listed under "Settled by agents" with rule `auto_delegated` and can be overruled. **Stale sweep**: every tick lists items nobody looked at for 3 days (`tick` → `stale`, `hermes_initiatives_core/review_sweep.py`), and the review agent keeps, refreshes, resolves, or (low/medium risk, recommended option only) applies each one with `daily_review.py sweep`. Refreshed options reach the user in the daily digest marked "refreshed", without a push of their own; applied and resolved items are settled under rule `stale_sweep` and can be overruled. The sync also closes `failed` and `awaiting_confirm` items once their source is done, archived or missing. **Waiting on you** (top of the Initiatives tab and the digest; `hermes_initiatives_core/review_attention.py`): every card whose newest handoff comment says `**You:** <a step>` (not "nothing …") while no open decision covers it, whatever its Review item's status: parked, watched, or never collected. Each row has **Start session to resolve**, which starts an `attend` session about that card (context: `GET /projects/<id>/attend-context?card=`). Every Review item (decisions, and the watching, deferred and auto lines) also has **Discuss**, which starts a `blocker` session that first explains the blocker in plain words: what it holds up, the actual cause, what was tried and decided, what each option does, and the recommendation (context: `GET /review/items/<Dnn>/session-context`, with the live card for a Kanban gate). The session does not answer the item. An item whose ask cites Markdown files under the project's vault or root (a plan to approve, its receipt, the cycle file) lists them under **Documents**; one click previews the file in place, read-only, with its path, bytes and SHA-256 (`GET /projects/<id>/doc?path=`, refused outside those roots), and every session the page starts is told to cite documents as Markdown links with absolute paths, which the desktop app previews in one click. Session titles lead with the item id (`D90 Blocker: …`); an attend session on a card that has a Review item does too (`D90 Attend: t_… …`). Once Discuss has started a session, the item lists its Discuss sessions (`GET /review/items/<Dnn>/sessions`) with Open, Retry and Discard. The `attend` action is for an approval whose next step the user does themselves: it posts their answer and a `**You:**` handoff to the card and parks the item until the card moves. `write` refuses a Kanban gate whose options all only park it, and a `defer` option labelled as an approval. Merging PRs from the review is not available yet. Store: `<plugin data>/reviews.db`.

### Reviews in Initiatives

Desktop and web have one **Initiatives** tab: each initiative card contains a **Reviews** subsection below its summary, and its detail page shows the same reviews. The tab count is the number of decisions and waiting-on-you cards, not the number of initiatives. **Needs your review** lists first every initiative with an open decision, a confirmation, or a card waiting on the user, most items first (ties keep rank order), each with an accent edge and pills that name what waits: "N to review" counts open decisions and confirmations, "N waiting on you" counts cards whose newest handoff asks the user to do a step. Those cards are listed first in the initiative's **Reviews** (and its detail page), with the step and **Start session to resolve**, as well as in **Waiting on you** at the top; the rest follow under **Other initiatives**, and inactive initiatives with nothing to review fold at the bottom. Flags are grouped by name (`waiting until 2026-10-31 +6`, `contract gap ×2`), worst first, with at most five per card and the rest behind `+N` (hover for every detail). **Unassigned reviews** is always present, including when empty; it holds reviews with no initiative or an initiative this project does not define. Unknown initiative IDs remain visible. All lifecycle states and confirmations remain reachable; review-wide diagnostics and desktop learned-rule controls remain below the list. Old web selections of the removed Review tab open Initiatives instead.

`GET /review?project=<id>` scopes live items and history before limits are applied and returns `initiative_groups`, keyed by each item's current initiative. Group collections contain short IDs; clients join them against the top-level lifecycle collections. Unscoped `/review` remains compatible. Grouping is read-only and does not migrate notes, answers or review history.

### Closed-loop outcomes

Adopted initiatives carry `outcome_policy` (enabled, owner, approval session). Each existing review tick
backfills missing contracts for new open cycles only within those adopted initiatives; it does not opt
unrelated initiatives in or change existing contracts. Enabled cycles carry `outcome_review` (owner,
release surface, collection procedure and approved nullable
target/review dates) and separate `assessment_cards`. The existing review tick queues bounded Kanban
assessment work after delivery or at a deadline. Workers publish real measurements, criterion/guardrail
gaps and a bounded follow-up proposal through `record_assessment.py`; the existing cycle Review item
then asks the user to conclude, iterate, change the contract, wait or stop. Missing evidence is reported
as insufficient data or measurement failure, never success. No new cron or database is added; Kanban
remains execution authority, and agent findings never set a user verdict or adopt a successor cycle.

Routine triage is delegated, not blanket approval. Every tick exposes `routine_candidates` from already-written
outcome options as well as new/changed items. The code-checked `plan_outcome_followup` rule can create one bounded
read-only investigation/repair-plan card against an existing user decision, never execute the proposed repair
or choose a product alternative. It settles only when the recommended option's signature is exactly
`cycle:outcome_review_ready:kanban_create:<plan lane>` and no option in the item sits on the review bar; one
verdict, disposition, stage, measurement or `record_decision` option anywhere in the item refuses the whole item.
`normalize_outcome_contract` can restore empty measurement fields only from
exact typed values in a proven prior user approval, not from prose or a planner's proposal. Both retain agent
audit receipts, source/basis checks and Overrule suspension. User-owned dates, criteria, scope, verdicts,
dispositions and protected operations remain decisions. An approved passing one-off spot check goes directly
to an evidence-backed closure question; optional empty template fields do not require a new contract and
another assessment round. Full predicates and invocation arguments: `skills/initiatives/references/review_tick.md`.

`migrate_outcome_loops.py` dry-runs by default. `--apply --session <id>` adopts the workflow for active
initiatives/open cycles only, preserving stages, measurements, results and history. Missing commitments
stay null and require a proposal; the migration never fabricates a deadline. `outcome_status.py` exports
stable derived signals for the existing wake gate, so a deadline can wake review without a file edit.

### Measurement readiness

Cycle edits through `edit_frontmatter` (including Review answers) validate before writing: new per-cycle errors are
refused, and a stage transition must satisfy all destination requirements. Review options are previewed with the same
rules before being offered. Existing invalid cycles can still receive audit rows and incremental repairs.

A measuring cycle needs a metric, source, baseline, and verified release. Use an observation window mapping
`{starts: verified_release | YYYY-MM-DD, length_days: N}` for time-based evaluation. A one-off check with agreed
`success_criteria` can leave the window null; no nominal one-day window is needed. Release observations remain facts:
recording one never discards it for missing contract fields, but returns `cycle_warnings`, and the collector opens a
`measurement_missing` review gate. Manual Markdown edits can bypass the writer; the validator and collector catch
those afterwards. No source, baseline, target, or release evidence is invented automatically.

## What it touches

Read this before installing; it is the disclosure the plugin catalog asks for.

- **Reads:** your initiative root(s), Kanban board databases (`mode=ro`), each lane's `state.db` for metrics, and cron
  job state/output for the profiles you configure.
- **Writes:** its own data under `~/.hermes/plugin-data/initiatives/`; initiative, cycle, idea and task Markdown files
  through locked, validated writers; Kanban only through the `hermes kanban` CLI when you (or a rule you delegated)
  apply an option.
- **Gateway hook:** `post_gateway_admission` reads messages in the configured review topic only; answer lines
  (`D3 A`) are applied by the executor, any other text passes through to the agent.
- **Network:** the Telegram Bot API (inline buttons and edits on pushed decisions) when `review_target` is set;
  GitHub (`gh`) and optionally `gcloud` from the recovery wake gate only when configured.
- **Credentials:** the Telegram bot token Hermes already uses (`TELEGRAM_BOT_TOKEN`), resolved through
  `agent.secret_scope.get_secret` / `hermes_cli.config.load_env`. The plugin never parses `.env` files and stores no credentials.
- **Who can answer:** button taps reach the plugin before the gateway's sender check, so the plugin checks the
  tapping user against `review_approvers` itself; typed answers need both gateway admission and an approver.
- **Background work:** none of its own. Cron jobs exist only after you run `hermes initiatives setup`; they run as
  ordinary Hermes cron jobs under that profile's tools and approval settings.
- **Private Hermes APIs:** none at runtime. Boards are found by the file layout the Kanban guide documents
  (`HERMES_KANBAN_HOME`, `kanban/boards/<slug>/kanban.db`). The one exception is the optional diagnostic
  `skills/kanban-sdlc-orchestration/scripts/probe_worktree_resolution.py`, which exists to exercise Hermes's own
  workspace resolver and is never run by the plugin.
- **Platform:** Linux and macOS (POSIX file locks).

## Develop and verify

```bash
env -u PYTHONPATH PYTHONPATH=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python -m pytest -q
~/.hermes/hermes-agent/venv/bin/ruff check .
hermes plugins validate .
env -u PYTHONPATH ~/.hermes/hermes-agent/venv/bin/python tests/bench.py $TMPDIR/ini-bench-large
node --check desktop/plugin.js && node --check dashboard/dist/index.js
# Before a release: no tokens, home paths or your own identifiers in the tree.
PRIVATE_PATTERNS='your-name|your-chat-id|your-project-id' scripts/check-public-tree.sh
```

Tests isolate `HERMES_HOME`, the Kanban home and plugin data; they never touch a real profile.

## License

MIT, see [LICENSE](LICENSE).
