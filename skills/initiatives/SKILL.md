---
name: initiatives
description: "Use when recording initiative observations, proposing ideas, filing tasks or bugs, or asking the user for a decision."
version: 0.1.0
metadata:
  hermes:
    tags: [initiatives, planning, measurement, kanban]
---

# Initiatives

An initiative sits one level above Kanban cards. It runs in **cycles**. Each cycle has a user decision,
Kanban delivery, a verified release, a measurement window, a verdict, and a disposition. The records are
Markdown files in an initiative root folder. Kanban stays the only execution authority.

This skill gives any agent, in any profile, scoped write paths and one check. Use only these scripts to
write initiative data. Do not edit initiative, cycle, idea, task, or bug files by hand.

## Who decides what

| Information | Owner |
|---|---|
| Release verification, measured values, evidence dates | Any agent, through `record_observation.py` |
| New ideas | Any agent, through `propose_idea.py` |
| New tasks (including bugs) | Any agent, through `report_task.py --title "…" --description "…" --type task` (or `--type bug`); `report_bug.py` remains supported |
| Card links (append only) | Any agent, through `link_cards.py`. Only the user removes a card or sets `excluded_cards` |
| Creation, rank, scope, success criteria, verdict, disposition, next cycle, idea promotion or rejection, task/bug statuses and linked cards | The user only (Tasks and Bugs tabs) |
| Missing evidence, stale observations, overdue evaluations | The dashboard derives them |

When the user makes a decision in a conversation, record it in the cycle or initiative file under
`decisions:` with `by: user`, the date, and the session ID. Record only what the user decided.

## Ask the user for a decision

Do not write an ask into a chat message or a cron report. File it in the daily review, so all of the user's
decisions are in one place. The same key never makes a second item; filing again updates the basis.

```bash
python3 SCRIPTS/request_decision.py --source <job or profile> --key req:<source>:<stable id> \
  --title "<short title>" --question "<one sentence>" \
  --option 'A|<what happens>|{"action": "handoff", "args": {}}' \
  --option 'B|Keep it parked|{"action": "defer", "args": {}}' --recommend A \
  --basis-json '<the state you asked about>' --context "<2-4 lines>" \
  [--owner-profile <p> --owner-job <cron id>] [--deadline <ISO time>] [--evidence <path or url>]
```

Kanban gates (`needs_input`, human blocks, triage), cycle gates, and ideas are collected automatically; do not
file them. `daily_review.py status` shows the queue.

With `handoff` options, the review does not apply the answer; your job does. On each run, read the answers
waiting for you, apply each one with your own tools, then acknowledge it with what you did:

```bash
python3 SCRIPTS/request_decision.py --answers --source <job or profile>   # [{short_id, option, note, ...}]
python3 SCRIPTS/request_decision.py --ack D17 --receipt "<what was applied>"
```

Use the user's `note` unchanged. A request filed with `--owner-profile` and no `--owner-job` (an interactive
session, which has no job to poll) is not left waiting: once answered, the 30-minute tick creates one Kanban card for
that profile on the project's board, carrying the question, the answer, and the note, and the worker acknowledges
the item with `--ack` when it has applied it. Do not answer review items yourself: answers come only from the user
(Review tab, or answer lines in the review topic, which the plugin applies before any agent sees them). The one
exception is an item the user marked **auto**: the review tick handles it with `daily_review.py auto` and appends a
delegated rule (`learned_<name>`) so items like it settle without the user next time (`references/review_tick.md`, "Auto
items").

## Find the root

The plugin settings file lists project roots (`$HERMES_HOME/../../plugin-data/initiatives/settings.json`
on a profile, `~/.hermes/plugin-data/initiatives/settings.json` at the root). Pass `--project <id>`, or
`--root <folder>`. With one configured project, both flags are optional.

## Record an observation

Use it for a production release check, a metric reading, a guardrail check, or a note on a cycle in
`decided`, `shipping`, or `measuring`.

```bash
python3 SCRIPTS/record_observation.py --project <id> --cycle <cycle-id> \
  --kind release_verified --environment production \
  --evidence <vault-relative report path> --detail "<what you checked>"

python3 SCRIPTS/record_observation.py --project <id> --cycle <cycle-id> \
  --kind metric --metric "<metric name from the cycle>" --value "<value with denominator>" \
  --evidence-date YYYY-MM-DD --evidence <report path>
```

Rules:

1. Use the cycle's `measurement.primary_metric` name for `--metric`.
2. `release_verified` means you checked production yourself. Merged is not released.
3. `--evidence` must point at the report or URL that shows the value.
4. To correct a row, record a new one with `--supersedes <at of the wrong row>`. The log is append-only.
5. The script fills `by` and `run` from your profile and session. Do not pass them.

## Close the outcome loop

An enabled cycle has `outcome_review`: a measurement owner, release surface, executable collection
instructions, and nullable approved target/review dates. Missing dates or criteria are gaps to propose,
not permission to invent commitments. New cycle plans must include goal, baseline, target, guardrails,
minimum sample, timeline, collection procedure and stop/budget conditions before delivery admission.
Plan the proof as delivery work, not a post-merge discovery: name the committed collector/fixture, its delivery
card or PR, and the command and receipt that prove it works before enablement. A collector described in a plan
is not implemented automation. Define owner, trigger, evidence and failure handoffs together with delivery.
Use [Proof and measurement handoffs](references/proof-handoffs.md) for the planning/closeout checklist.

The existing review tick queues bounded **assessment cards**, separate from delivery cards. They verify
production, main CI, runtime or the named manual surface as appropriate; collect real evidence; compare
it with every criterion and guardrail; and write a bounded next-plan proposal before the user is asked.
An assessment finding is not a verdict. Agents cannot adopt a new goal, deadline, verdict, disposition
or successor cycle, nor implement a proposed follow-up before its approval.
The existing Review tick may normalize empty measurement fields from exact typed prior user approvals or
create one bounded read-only investigation/repair-plan card under code-checked delegated rules; neither is
permission to adopt a proposal or execute protected actions. See `references/review_tick.md` → Routine work.
For an approved one-off spot check, evaluate the actual criterion from real evidence. Empty optional template
fields alone do not require a new metric/window or another approval round before the user can close it.

Assessment cards carry `Assessment cycle: <cycle-id>`, not the delivery marker below. The dispatcher
links them to `assessment_cards`; never add them to `kanban_cards`. A failed or completed-without-proof
assessment is visible in Review, not silently retried into an unlimited card chain.

Inspect the **current** epoch before collection. Begin both new artifacts with the exact line
`Assessment epoch: <collection epoch>`. Re-inspect after recording metric/release observations and before
the receipt. If inputs changed, recollect against the new contract; never relabel an interim report for a
mature window. Unfinished assessment cards keep ownership across input changes; do not create a duplicate.
Record both existing, nonempty absolute artifact paths:

```bash
python3 SCRIPTS/record_assessment.py --project <id> --cycle <id> --inspect
python3 SCRIPTS/record_assessment.py --project <id> --cycle <id> --epoch <current epoch> \
  --status missed --evidence /absolute/gap-assessment.md --proposal /absolute/next-plan-proposal.md \
  --detail "<measured gap, limitations, recommended next decision>"
```

Statuses: `met`, `missed`, `insufficient_data`, `measurement_failed`. Quote approved targets; distinguish
missing evidence from a failed target. A fully observed mature window below target is `missed` (shortfall),
not `insufficient_data`, unless an explicitly approved minimum-sample requirement makes it unevaluable. Preserve
that requirement; never reinterpret a low usage target as an evidence minimum or change approved semantics.
Reports identify exact SHA/run/cohort/window, denominators,
comparability, criterion-by-criterion deltas and untested guardrails. Proposals state expected benefit
(mark estimates), cost/budget, risks, scope, timeline and stop conditions. If collection fails, publish
that limitation and the bounded remedy; never synthesize plausible data. The receipt hashes the artifacts.
The existing cycle Review gate then carries the report and proposal for the user's decision.

## Link cards to a cycle

Every Kanban card made for a cycle carries this line in its body:

```
- Initiative cycle: <cycle-id>
```

Right after you create the cards, link them in one call and check that the output says `"ok": true`:

```bash
python3 SCRIPTS/link_cards.py --project <id> --cycle <cycle-id> --card t_xxxxxxxx --card t_yyyyyyyy
```

Rules:

1. The script only appends. It skips cards already listed, and refuses unknown, off-board and excluded cards.
2. Do not leave the link to "the operator". The page flags a marked but unlinked card as `cards_unlinked`, and
   the daily review's `collect` step links it (`link_cards.py --sweep`), but that is a backstop.
3. A prose mention of the cycle does not link a card. Only the body line does.

## Propose an idea

```bash
python3 SCRIPTS/propose_idea.py --project <id> --title "<short title>" --size cycle \
  --related-initiative <initiative-id> --problem "<observed problem>" \
  --expected-outcome "<what would change>" --evidence <report path>
```

Rules:

1. Read the open ideas first. The script lists similar ideas in `similar_to`; mention a duplicate instead
   of proposing it again.
2. Each source may hold at most 5 open ideas. Propose fewer, better-evidenced ideas.
3. An unapproved task-sized proposal still goes here. Concrete work the user asks to track goes through `report_task.py`; the user decides whether it becomes a Kanban card.

## File a task (including a bug)

```bash
python3 SCRIPTS/report_task.py --project <id> --title "<short title>" \
  --description "<what needs doing or what goes wrong>" --type task --priority medium \
  [--related-initiative <initiative-id>] [--evidence <path or url>]

# A bug is a task type; its description is also the symptom unless supplied separately.
python3 SCRIPTS/report_task.py --project <id> --title "<short defect title>" \
  --description "<observed behavior>" --type bug --priority high \
  [--symptom "<exact error or observed value>"] [--expected "<what should happen>"] \
  [--repro "<steps or command>"] [--where "<page, file, or service>"]
```

Rules:

1. File a concrete task when the user asks to track work. Use `--type bug` when tracking a defect the user asks to
   log or one you find outside the current task's scope. A defect in the code you are changing belongs to that
   task; fix it there. Proposals whose scope or outcome still needs deciding belong in the ideas inbox.
2. Records open as `status: open` in `tasks/task-YYYYMMDD-<slug>.md`. The user decides statuses and card creation
   in the **Tasks** tab (bugs: the **Bugs** tab). Do not triage, mark done, cancel, or create/link a Kanban card unless the user asks.
   **Create card** links the card and marks `in_progress`; cancellation requires a reason, and completion requires
   a card or reason. Kanban remains the execution authority.
3. Read `similar_to` in the output. If an open record describes the same work, mention it instead of filing again.
4. Describe observed facts. For bugs put guesses at a cause in `--body`, explicitly marked as guesses.
5. `report_bug.py --title "…" --symptom "…" --severity medium` still works and creates legacy `bugs/bug-*.md`
   records. Existing bug IDs, notes, links, and sessions remain usable in Tasks; no manual migration is needed.

## Check the data

```bash
python3 SCRIPTS/validate_initiatives.py --project <id>
```

`SCRIPTS` is the `scripts/` folder beside this `SKILL.md`.

Kanban health for a project's board (delivery, failed runs by cause, review rounds, tokens, blocked age, each in
local-time buckets with the previous equal window) is the page's **Metrics** tab, or
`GET /api/plugins/initiatives/projects/<id>/metrics?days=7|14|30|90`. Read it before quoting board throughput.

## Cycle writes are validated

Never hand-edit a cycle file: every write through the plugin's writer validates the cycle first, and a change that
would add a validator error is refused with `WriteRefused` and nothing is written. A stage change must satisfy every
requirement of the stage it enters. Errors the file already had do not block unrelated edits, so incremental repairs
and audit rows still go through.

A cycle reaching `decided`, `shipping`, `measuring`, or any verified release needs `primary_metric`, `source` and
`baseline`, plus an `observation_window` mapping `{starts: verified_release | YYYY-MM-DD, length_days: N}`. A one-off
check with agreed `success_criteria` may leave the window `null`; never invent a nominal window. Never invent a metric,
source, baseline or target: ask the user, through the cycle review item, and record their answer as one option.

`record_observation.py` always keeps a true observation, even on an incomplete contract; its response then carries
`cycle_warnings`, and the review collector opens a `measurement_missing` item naming the fields. That item opens as
soon as the cycle is admitted (`decided` or `shipping`), not only after a verified release, so the plan is recorded
before delivery; validation itself errors from `measuring` or a verified release. Admission is its own item
(`cycle:<project>:<cycle>:measure`) because card items keep exact `reasons` that settle rules match strictly; a cycle
that also lacks `success_criteria` waits behind the initiative's criteria gate, so one gap gets one item. Triage may
hold an admission item in `watching` when a recorded user decision already states the window in prose; the write-time
check still refuses that prose at `measuring`, so it gets translated into `{starts, length_days}` then. Manual Markdown
edits bypass the writer; the validator and the collector catch those afterwards.

