# Reviewing a recovery cron tick

Use for "review the last recovery run", "review recent recovery runs" or "why are cards still blocked after it ran". Prove what the ticks did before you judge the cards.

## Procedure

1. Find the job and its last run. Grep `cron/jobs.json` and `profiles/*/cron/jobs.json` for the job name. Read `last_run_at`, `last_status`, `failure_streak`, `no_agent`.
2. Read the delivered text from `profiles/<p>/cron/output/<job_id>/<newest>.md`. That file holds only the prompt, the loaded skills and the final response. It does not show actions.
3. Read the actions from the session transcript in `profiles/<p>/state.db` (open read-only), table `messages`, `session_id = 'cron_<job_id>_<YYYYMMDD_HHMMSS>'`. Print each assistant `tool_calls` entry and a short slice of each `tool` result. Look for refused tools (an unattended session blocks `execute_code`), lifecycle transitions and comments.
4. List the board events from the tick start until now: `task_events` with `created_at >= <tick start>` and `kind != 'heartbeat'`. Attribute each one to the tick, a worker, the dispatcher or an interactive session. A tick that wrote no events only reported.
5. Re-classify every card in the delivered report from live state (`SKILL.md` → Gate classes). The report reuses stored reasons and diagnostics, and these can be older than the newest review round or user decision.
6. For each `ready` card that does not start, apply the dispatch causes in `SKILL.md` → Running-card health step 5. For `active_pr`, compare the time of the newest comment with a PR URL against the newest `assigned`/`changes_requested`/`review_reopened` event (guard logic: `_respawn_guard_reason` in `hermes_cli/kanban_db_dispatch.py`). If no handoff follows that comment, the guard ends at that comment's time plus `_RESPAWN_GUARD_PR_WINDOW`. Report that time as the self-release.
7. Measure the stall and find the root blocker. Read the newest `claimed`/`spawned`/`completed`/`unblocked` event time and the newest `task_runs.started_at`; hours with no claim and zero `running` cards means the cron is only re-confirming. Then, for each `todo`/`blocked`/`scheduled` card, list parents not in `done`/`archived` (`task_links` parent_id → child_id). Follow the chain up to the card that gates the most descendants and report that card first, because unblocking it moves the whole chain.
8. Check prompt-policy drift. Compare each rule the cron prompt states (consent waits, round cap, risk classes, what counts as a defect) with the current version of the policy skill it loads. The prompt is not updated when the policy bumps, and a stale rule makes the tick misclassify the same card on every run.
9. Find refusal loops. The same `REFUSED <card>: <reason>` from the route script on every tick, or one card that gathers a `respawn_guarded` event every minute, means the tick has no working route for that card. Fix the route or the prompt; the cards are not the problem.
10. Check for re-created work. List cards created since your last review (`tasks.created_at`, `created_by`). A new repair or planning graph for a gap the user already withdrew means the prompt or a shared reference still classifies that gap as a tooling defect. Report its run count and agent-hours, and recommend stopping it together with the prompt fix.
11. When the cron's block comment names the wrong cause, post an `FYI` correction on the card, and on any repair card it was routed to. Open with "Correction to comment #<n>", put "Supersedes" in Evidence, and add no transition or acceptance criteria. Never edit the old comment.

## Tracing one card across ticks

For "why did the cron not unblock <card>":

1. Count the job's output files that name the card (`grep -l <card> cron/output/<job_id>/*`). They show only the reported gate text.
2. Read the reasoning from `state.db`: `messages` where `session_id LIKE 'cron_<job_id>_%'`, `role = 'assistant'`, and `content || reasoning || reasoning_content || tool_calls` contains the card id. Skip rows that are only the final gate digest. The reasoning line states the tick's classification (for example "policy-gated hold, stays untouched").
3. Compare that classification with the prompt's recovery rules (`jobs.json` prompt) and the policy line it cites. A correct hold under the rules is a policy question for the user, not a cron defect.

## Reviewing many recent ticks

For "review recent recovery runs", aggregate before you read any one tick:

1. Read the job state from `jobs.json`: `state`, `enabled`, `paused_at`, `paused_reason`, `next_run_at`, `last_dispatch.kind`. A pause with a null reason is a finding. Report it and say whether you could find who paused it.
2. Run history: `hermes --profile <p> cron history <job_id> --limit N`, plus the `executions` table in `profiles/<p>/cron/executions.db` (`status`, `delivery_outcome`, `scheduled_instant`; a null `scheduled_instant` marks a manual or out-of-schedule run). Read open incidents from `cron_incidents`. Gaps between `scheduled_instant` values show missed ticks.
3. Per-tick cost and size: the `sessions` table in `state.db` (`message_count`, `tool_call_count`, input/output tokens, `started_at` to `ended_at`). Compare the duration with the schedule interval.
4. Delivered text: slice everything after `## Response` in each output file. Most ticks are `[SILENT]`, so print only the non-silent ones in full.
5. Mutations: count operator comments per card from `task_comments` (`author = <operator profile>`) over the window. Do not regex the transcript `tool_calls` for verbs, because `--help` lookups match too. One card with a comment on almost every tick is a no-progress loop.
6. Refused tools: grep the profile `logs/agent.log*` for `cron_<job_id>_<date>` plus `BLOCKED:`, and count by type (`execute_code`, security-scan pattern). Repeated refusals are lost steps on every tick, and a prompt rule fixes them.

## Prompt fixes that hold

Back up `jobs.json` under `profiles/<p>/skill-backups/` first. Write the old prompt to a scratch file, apply each replacement in Python with `assert p.count(old) == 1`, then edit with `hermes --profile <p> cron edit <job_id> --prompt "$(cat <file>)"` and read `jobs.json` back and compare the prompt byte for byte. Resuming a job whose `next_run_at` is in the past starts a catch-up run on the next scheduler tick. Verify the fix on the next tick: read its transcript while it runs (the session row exists before `ended_at` is set) and the target card's status and newest comment, then report the transition and the follow-on claim.

- **Predicate rules, not record rules:** when a prompt rule tells the tick to look for a grant, approval or "new decision", rewrite it to test the artifact the gate protects (file lines present, PR state, installed version), with the evidence source and the transition to take when it holds. Delete the stale rule in the same edit; two rules for one gate make the tick pick either.
- **Name the working route, and forbid the repair:** when a gap has an existing route (see SKILL.md, local-only routes), write the exact command shape into the prompt and add "do not create or route cards that repair <gap>". Fix the same sentence in the shared skill reference the lanes read (`kanban-sdlc-orchestration/references/recovery.md`), or the orchestrator keeps admitting repairs. A shared skill script change needs its own unit test under `scripts/tests/`, run RED before the fix; back up the whole skill directory first.
- **Check each new sentence against the primitive:** before saving, verify every "X lands in Y" claim against the installed function (for example, `unblock_task` sends `scheduled` to `ready`, never to `review`). A wrong route in the prompt fails on every tick.

- **No repeat comments:** comment on a card only when its state, evidence or recommended action changed since the operator's newest comment on it. Timestamps, tick ids, scratch paths and plan-file names are not changes. A card that waits on another card's decision gets one comment that names that card.
- **Tool shape for unattended runs:** use `terminal` for every command. Do not call `execute_code`. Do not pipe a command into an interpreter (`cmd | python3`). Do not start a process from inline Python (`subprocess`, `os.system`). Redirect each CLI output to a scratch file, then parse the file with inline Python. Each refused shape is a lost step on every tick.

## Answer shape

- One line: what the tick did (mutations, or report only), its duration against the budget, and its token cost. On a static board, a long run of `[SILENT]` ticks is a cost finding: state tokens per tick and the time since the last state change.
- The stall chain as a small diagram when one card gates many (root blocker → descendants).
- One row per reported card: report says / real cause now / who acts.
- Every card where the cron's own classification or block comment is wrong, with the correct cause.
- The concrete next actions you can take, as an offer.

## Pitfalls

- A process in a parked card's worktree is not always a stray worker. Run `pstree -s -p <pid>` and read the parent's `cwd` and `HERMES_HOME`. A plain `hermes` parent with no kanban worker scope is an interactive session doing the parked step. That is progress, not a stuck card; do not signal it.
- A diagnostic such as `repeated_crashes` stays on the card after later successful review rounds. Do not report it as the live cause.
- A cron assessment that ends "no newer grant" or "no new decision" checks the record, not the predicate. Re-probe the artifact itself (file content and hash, PR state, installed version) before you accept the cron's "still blocked" verdict.
- A cron "cleared predicate" unblock checks only the gate named in the newest block reason. Before you accept a cron `RECOVERED` on a multi-gate card, re-check every prerequisite the card body names; if one is missing and no claim exists yet, `schedule` the card and comment why.
- Session ids in `state.db` carry the tick's dispatch time (`cron_<job>_<YYYYMMDD_HHMMSS>`), and the output file carries the finish time. Match a report to its transcript by the job id and the nearest earlier session start.
