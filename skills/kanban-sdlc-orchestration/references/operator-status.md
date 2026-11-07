# Operator Observability and Board Status

Read this for status reports, questions about why a card is stopped, human replies, notification routing, and the recovery/report cron. Lifecycle repair procedures are in `recovery.md`.

## Probe toolkit

- Start with `scripts/kanban_board_status.py`. It is a read-only digest of each card's status, block kind, breaker counters, newest event and notify coverage.
- Minimal read-only set (`hermes kanban --board <board> ...`):
  - `show <id>`: the authoritative card view.
  - `runs <id> [--json]`: prior attempts vs the active attempt.
  - `list --status <one-status> [--json]`.
  - `diagnostics [--severity error] [--json]`: breaker state without touching the DB.
  - `stats --json`.
  - `notify-list [--json]`.
  - `dispatch --dry-run --json`.
  - `log <id> --tail <bytes>`.
- `list --json` returns a bounded window, so never state board totals from it. Use the per-status counts in `hermes kanban boards list --json`.
- `block_kind`, recurrence counters and event payloads exist only in the DB. Open it read-only (`file:<board>.db?mode=ro`) and check `PRAGMA table_info` before naming columns, because the schema varies by release.
  - List the tables first (`sqlite_master`) instead of assuming names.
  - Parse actors from event payload JSON, not from columns.
  - Compute ages from heartbeat, run, and event timestamps, and confirm each timestamp's unit before subtracting.
- A JSON decode failure is not a missing card. Fall back to plain `show` and parse leniently (`strict=False`).
- In unattended jobs, if a probe path is denied, save CLI JSON to a uniquely named scratch file and parse it with a separate read-only command. Do not change approval policy to bypass the denial. File-backed summaries also keep large card histories out of context.
- A quiet `log` is not inactivity; check heartbeat and PID. `log` truncates long results, so read full refusal/verdict text from the lane's own `agent.log`/`errors.log`.
- Treat quota caches and combo definitions as diagnostic hypotheses, not current lane availability. A fresh successful model turn or active worker completion calls supersede an older no-target/quota diagnosis; keep older evidence as history and do not infer that a different lane recovered too.
- Distinguish absent subscriptions from `wake`-only subscriptions: the latter can wake an operator but do not prove a user notification was sent. Read `delivery_mode` before reporting coverage.
- Separate `list` calls are not an atomic snapshot. Verify each named target with `show` plus its latest run.
- Never resume an active worker session. Use a redacted `sessions export` for a transcript.
- Confirm flags with `<verb> --help` before relying on them. Never guess.
- Read these references and the project policy before replaying a guard, breaker, or dispatcher rule from the installed Hermes source. Local patches change behavior and are recorded only in the policy, so a source-only reading produces a confident and wrong forecast of what a guard will do.

## Why is X blocked

- Read the live card and its runs. Re-probe the named external prerequisite (tool, credential, branch, PR, approval) without mutating anything, because blocker text outlives its cause.
- Report three things: the recorded reason, whether it holds now, and the exact next gate.
- Classify the stop mechanism before proposing a release:
  - **Sticky block:** the newest `blocked`/`unblocked` event decides. Only `dependency` blocks clear by themselves (from `todo`).
  - **Recurrence limit:** repeated same-reason unblock/re-block routes the card to `triage` with `block_loop_detected`. Fix the cause, then spend one deliberate reset.
  - **Breaker trip:** `gave_up` + `protocol_violation` means the retry budget is spent, not a new defect. The `gave_up` payload shows failures, limit and limit source.
  - **Respawn-guard hold:** a high `respawn_guarded` count means a healthy dispatcher refuses this card. Read the guard reason.
  - **Block kind:** `needs_input`/`capability` are human gates. `transient`/`dependency` are machine-recoverable but still hit the recurrence limit.
  - `blocked` with NULL `block_kind` and no block event means a terminal run outcome, not a human gate.
- A run that dies in about a second with a turn-budget or empty-verdict message is a lane-capability defect. Repair the lane; do not retry.
- **A shared provider error is a dead lane route, not N broken cards.** When several cards on one assignee fail with the identical provider message, probe that lane's model alias and each of the alias's targets separately before retrying any card, because a failover alias with one dead target can return that target's terminal error instead of failing over, so the whole alias fails while each live target answers alone. A lane pinned to a broken failover alias dies on every run, so each card looks individually broken and each trips its own breaker. Repair the route, then spend one reset and confirm the claim before resetting the rest.
- **Many cards crashing before the model call is one environment fault, not N card defects.** When cards on different lanes all die at worker admission with the same refusal, check whether the gateway predates the latest install update before reading any card (`recovery.md` → "Gateway/worker code skew after an install update"). Answer "why are the cards crashing" with the proven mechanism (failing line, code diff, a probe showing the mismatch), not a likely cause.
- Nothing automatic releases a `blocked`/`scheduled` card. Only CLI `unblock` and dashboard `Blocked → Ready` do. The only automatic promotion is dependency promotion out of `todo`.
- When asked "does anything auto-recover?", audit scheduled jobs separately from the board:
  - Read each job's prompt, `no_agent` flag and mandate. A job's name does not tell you whether it mutates.
  - Check its failure streak, last error and last success.
  - Identify which cards fall within its mandate.
  - Agent-mode jobs can die at startup while script alerts in the same profile keep firing.

## Board status reports

Lead with a causal inventory:

1. Count every status from the authoritative store. Give the exact literal `blocked` count, then separate counts for `scheduled`, `triage` and non-terminal error/critical diagnostics.
2. Classify each stopped card:
   - human/external gate
   - breaker
   - iteration exhaustion
   - protocol violation
   - resource/process loss
   - closeout bookkeeping
3. Check the dispatcher owner process and recent claims before saying the dispatcher is down. One dispatcher-owning gateway can spawn every lane while the per-lane units are stopped.
4. Separate delivery state from ledger state. Merged-and-reachable work with blocked closeout metadata counts as delivered, not unfinished.
   - **Resolve which ledger the user means first.** "In progress" or "open" in a question may name a status in a linked tracker (an initiatives/task store, an issue tracker), not a Kanban status. If the board shows little running work, count the linked tracker's statuses and join each item's card id to the board read-only. A tracker that sets `in_progress` when it creates a card, with no closer that fires when the card finishes, piles up items whose cards are long `done`. Report it as stale ledger state with the missing closer named, not as active work.
5. Put the dominant shared cause first. Then list exceptions, the bounded action that releases each group, and which cards need a human versus a machine predicate.

Report two layers under separate headings:

- **Card state:** status, block kind, retries vs limit, last failure, next gate.
- **Live external state:** re-resolve the branch head, the newest run for that exact SHA, open PRs and promotion state.
  - Report CI per exact merge SHA. A run cancelled by a newer push is unverified; a pending run is not green.
  - Per-SHA main CI is live external state, not a card gate. It never holds or reopens a merged delivery card; it is handled by the dedicated CI workflow or SHA-scoped repair cards.
  - A merge does not satisfy review or inspection gates.
- **Drift:** a SHA-scoped repair card whose SHA is now an ancestor of the head (`git merge-base --is-ancestor`) neither validates nor covers the current head.
  - If the head is green, report the card as historical.
  - Never reopen the card or reuse its idempotency key.
  - If its PR's changes are absent from the branch, superseding the PR is the user's decision.
- **New defect with no card:** this is new intake that needs user authority (`intake.md`). Never attach it to a superseded card.
- PR state `UNKNOWN` means not yet computed and `BEHIND` means the branch lacks newer target commits. Neither is a conflict, and `BEHIND` needs no action unless project policy requires up-to-date branches. Re-read.
- For each human-gate card, confirm a subscription row exists, its notifier gateway is live and its target resolves before implying the user was alerted.

## "The board seems stuck"

Use this order. Each step answers one question in a few commands.

1. **Did recovery run, and does it cover these cards?** Run `hermes cron list` and read the recovery job's last run, status and last failure. Read its own `enabled`/`paused_at` as well: a disabled job keeps reporting an `ok` last run and no failure streak, so it reads healthy while it is the reason nothing recovers. Before you name a paused job as a cause, read its prompt mandate (`cron/jobs.json`) and compare it with the stuck cards. A job named for a project can own only one card graph, so resuming it releases nothing outside that graph. Say which cards it covers. Then read its newest outputs under `<profile>/cron/output/<job_id>/<ts>.md`. Output files may start with the full prompt, so print only the response section. Scan backward until you find the last non-silent tick. That tick is the last alert the user received.
2. **Is there runnable supply?** Read the per-status counts in `boards list --json`. If `running`, `ready` and `review` are all 0, the dispatcher is idle because no work is runnable. It is not a dispatcher fault and not a cap problem.
3. **Which card holds the most work?** Read `task_links` joined to `tasks` read-only for every non-terminal child. A linear chain of `todo` cards behind one parked parent makes the whole board look stuck. Report that parent first, with how many cards wait behind it.
4. **Trace each machine block to its owner.** A `BLOCKED — MACHINE` card that waits on an external predicate (main CI, another PR, test data) is only as live as the owner of that predicate. Read that owner's latest output (the CI or main-sync job's last agent response, or the PR state and age). If the owner already waits on a human decision, report the card as human-gated through that decision. If no card, job, or lane actually owns the predicate (the comment says "the runtime owner must …" and nothing matches), report it as an **orphaned machine block**: it never clears, recovery correctly leaves it alone, and its Review item sits `watching` on a `machine:` ground for ever. Count the cards behind it and offer one owner card. The change-only reporter drops `BLOCKED — MACHINE` cards, so it never alerts on either transitive gate.
5. **Check alert coverage.** For each card that really waits on the user, confirm a notify subscription exists. A card with no subscription is silent even when it is gated.
6. Report: the root gate cards, the chain each one holds, every transitive human decision with its source job, and the exact choices. If you cannot find the recovery tick's session export, say that you did not verify its actions. Do not infer them.
   - Name each human gate's Review item id (`Dnn`, found by card id in the initiatives `reviews.db` `key`/`links_json`) beside the card, so the user can act from the Review tab instead of hunting for it.
   - For a breaker-tripped card, re-test the crash's precondition now (for example, does the module or file the worker could not import exist in the current install?). Say whether it still holds, and that one deliberate reset with a claim check is the test. A trip that predates an install update may already be fixed.
   - Re-read board counts immediately before sending a status reply. The user can answer gates from the Review tab or dashboard while you probe, so a card you list as blocked may already be running. Correct an earlier report plainly in the next reply.

## Concurrency supply vs cap

- The cap is a ceiling. Establish runnable supply first: `stats`, `list --status running`/`ready`, `diagnostics`, `dispatch --dry-run --json`.
- Only `ready` cards are claimable. A cap is implicated only if ready work exists and the dry run shows cap skips.
  - `todo` is parent-gated.
  - `blocked`/`scheduled`/`review` are gates, not spare supply.
- `dispatch --max N` caps spawns in one pass; with N already running it may spawn none. Verify each claim.
- Never unblock dependents to fill slots. Release a card only when its prerequisite is proven false or the user explicitly supersedes it.
- Config controls a shared board only through the gateway home that actually dispatches it. Resolve that from the live process's identity keys; never dump the full environment.
- When asked what limits apply, report both layers in one answer: the dispatcher config (`max_in_progress`, `max_in_progress_per_profile`, `failure_limit`, stale timeout, review dispatch) and the project policy's capacity rules (resource-class places, preview/server slots, build serialization, worker memory caps). Policy limits are enforced by admission rules and wrapper scripts, not by config, so a config-only answer misses them. Add live occupancy (running count, slot holders) from a read-only probe.

## Process identity probes

- A terminal run does not prove the process exited, and a fresh heartbeat proves liveness, not progress.
- Prove identity with `ps -p <pid> -o pid,ppid,pgid,sid,stat,etimes,cmd`, `readlink -f /proc/<pid>/cwd` and a descendants query. Require the exact task command and workspace with no active claim.
- Never signal on stale PID, profile or cwd alone. Signals need fresh, operation-scoped approval; if approval is denied or times out, assume no signal was sent (`recovery.md`).
- To detect a loop, count distinct commands and refused results, not log volume. Get the rejected argument from the exported transcript. Classify it as a lane defect, because waiting spends another full budget.

## Human replies

- A comment is durable context for respawned workers. It never changes status. "approved" or "retry" in a comment is not an unblock.
- `unblock <id> --reason "..."` returns the card to its safe source phase:
  - `review` for reviewer-origin work
  - `ready` if its dependencies are done
  - `todo` if a parent is still open

  It accepts only `blocked`/`scheduled` (for `triage`, see `recovery.md`).
- Re-read immediately before mutating. If another actor already moved the card to `ready`/`claimed`/`running`, do nothing, because a repeat transition can reclaim a healthy worker.
- `ready` means queued. Report "started" only after a new `claimed`/`running` run with a live PID. An occupied lane is not a dispatch failure.
- "Unblock those": enumerate the live candidates and separate the shared hold from each card's own gate. If the referent is ambiguous, ask one scoped question. Never release inspection, plan, merge or dependency gates by inference.
- "Approve all" covers only the gates that the immediately preceding report enumerated. Record a user-authored decision on each card before transitioning it. It never covers `changes_requested`, correctness findings, infra failures, missing exact-SHA evidence or dependency waits (`gates.md`).
- TODO is not a parking lot. In the dashboard, drag `Blocked → Ready` (which runs unblock), never `Blocked → TODO`.
- Never `complete` a `blocked`/`scheduled` card. Unblock it and let the owning lane reconcile.
- Serve the dashboard on loopback only unless a deliberate auth design exists.

## Notification routing

- A home channel is a default destination, not a card subscription. Only a `kanban_notify_subs` row routes a card's events.
- Cards created with `/kanban create` in chat auto-subscribe when `kanban.auto_subscribe_on_create` is on, and linked children inherit routing. Anything else needs an explicit subscription:

  `notify-subscribe <id> --platform --chat-id [--thread-id] --user-id --chat-type --notifier-profile <gateway owner> --delivery-mode notify+wake`
- Verify the row field by field with `notify-list --json`.
- A new subscription does not replay past events. If the card is already blocked, send one current-state message: board, id, title, reason, and inspect/comment/unblock commands.
- To bulk re-point, read ids read-only and run `notify-subscribe` per card. Never write the DB.
- A row is not proof of delivery. Thread and topic ids are not durable, and dead threads degrade silently (threadless fallback or outright failure). Prove delivery from the cron `delivery_outcome` and an actual receipt. Re-pointing needs user consent.
- For visual-inspection gates, send screenshots as native media (`MEDIA:/abs/path.png`), not file paths.

## Change-only human-gate alerts

- **Gate set:**
  - Include `blocked`, `scheduled` with a parking reason, and non-terminal error/critical diagnostics.
  - Exclude `review`, `ready`, `running`, plain `triage`, `todo` and reasonless `scheduled`.
  - Surface repeated `respawn_guarded` holds through status reads instead.
  - Drop a gate when the newest format-conformant statement at or after the gate event says no human action is needed. That statement is the event reason or a later card comment containing `BLOCKED — MACHINE` or `**You:** nothing needed`. A later `BLOCKED — NEEDS YOU` or any other `**You:**` ask keeps the gate. Unformatted text keeps it too, so the reporter fails toward alerting.
  - Age-based diagnostics may date a card from the wrong event. Re-derive ages from the newest event that re-queued the card. Do not report a card stranded while the host-wide in-progress cap is full, because the cap holds it, not a missing worker.
- Hash only the stored gate reasons into the change digest, never diagnostic titles, because titles carry growing ages and counts that re-alert every tick.
- Read `block_kind` and the waiting reason read-only from the DB, because the JSON commands omit them.
- Run it as a script-only (`no_agent`) job. Its stdout is the delivery contract: empty means unchanged.
- Compare against the last notified digest, not the last seen one, so changes during quiet hours are delivered later. Persist the entry set to mark NEW and RESOLVED.
- Report internal failures as alerts with a stable digest and exit 0. A silent or out-of-window failure is worse than a visible "monitor failing" alert.
- Each alert is one line per card plus one copyable command. Test with injectable fixtures (a synthetic DB and scratch state dir), never a real blocked card.
- Silent output means "unchanged since the last notify", not an inventory. Never report "no human gates" from it.
- The reporter trusts the card's own `BLOCKED — MACHINE` statement. A machine block whose predicate waits on a human decision raised elsewhere is invisible to it; status reads must trace it (see "The board seems stuck").

## Agent-mode recovery cron

1. Investigate with read-only probes.
2. Do only owner-safe recovery within the job's mandate: re-read before each mutation, fail closed on ambiguity, and send no signals without fresh approval.
3. Run the change-only reporter as the last action, exactly once per tick.
4. Return its stdout verbatim, or `[SILENT]` if it is empty.
5. Never re-run the reporter to freshen the digest: its first invocation marks those gates as notified, so a second invocation in the same tick prints nothing by design. Return the first non-empty output, and never read the later empty run as "no human gates".

- Keep probes free of side effects on fixed paths, because any refused write appears as a warning in the delivered message.
- **Resuming a paused job:** run `hermes cron resume <id>`. The job keeps its old `next_run_at`, so `cron list` shows it as `Overdue` by days. That is normal: the scheduler fires it on its next pass and then reschedules it from the current time. Do not run it by hand on top of that. Confirm the resume worked from a new `cron/output/<id>/<ts>.md` file and its response section, not from the `[active]` flag alone. Then report three things: the job's mandate scope, its delivery target (`local` means the user gets no alerts), and its model cost per tick.
- Job execution records hold status and delivery outcome, not the agent's actions. Read the actions from the job's exported session.

## Card comment format

Every lane (implementer, reviewer, operator, orchestrator) writes card comments and run summaries in this shape. A human must learn the outcome, their own action, and what happens next from the first three lines; audit detail goes below the rule.

```markdown
**<VERDICT>** — <one plain sentence: what happened>

**You:** nothing needed | <exact decision or action, one line>
**Next:** <who does what next, and whether it is automatic or waiting>

- <at most 5 bullets: key facts in plain language>
- PR #<n> @ `<8-char sha>` · CI ✅/❌/⏳ · review ✅/❌/⏳

---
**Evidence**
<full SHAs, CI run IDs, commands, test counts, PIDs, paths, prior-SHA history>
```

- **Verdict** is one of: `READY FOR REVIEW`, `CHANGES REQUIRED`, `APPROVED & MERGED`, `BLOCKED — NEEDS YOU`, `BLOCKED — MACHINE`, `RECOVERED`, `UNBLOCKED`, `FYI`. A fixed vocabulary makes the card scannable.
- **Top part** (above `---`): at most about 600 characters, using 8-character SHAs and no run IDs, PIDs, or paths. Translate internals into plain words: write "the dispatcher won't restart it while PR #<n> is open", not `respawn_guarded: active_pr`. Name the human only when the human has a decision or action to take.
- **Findings** (`CHANGES REQUIRED`): use a numbered list with one finding per item, stating what is wrong, where (`path:line`), and what fixes it. Put the reasoning in Evidence.
- **Evidence**: put full 40-hex SHAs, CI run IDs, and exact commands here. These rules govern prose only; the exact-SHA evidence and manifest rules are unchanged. If the Evidence section would exceed about 2,000 characters, attach a report file instead and reference it.
- **Operator tick receipts**: the same limits apply. The top part is everything above the `---` rule, so budget about four short bullets and keep run IDs, PIDs, and full SHAs out of it. Put graph and external-state readback in an attached file, not the comment.
- **Prose style**: write the top part, bullets, and findings in ASD-STE100 style, about 80% of the full specification. Use short sentences (max 20 words for an instruction, 25 for a description), one fact or instruction per sentence, the active voice, and one term for one thing. Write actions as commands. Keep technical names exact. Relax a rule only when strict STE would lose precision, a tradeoff, or a degree of uncertainty; state uncertainty directly ("not verified"). Evidence keeps its exact raw form.
- **Diagrams**: the card view renders Markdown but not Mermaid. When a dependency order, flow, or state change is easier to read as a picture, add a small text diagram (about 12 nodes or fewer, every arrow labeled) in a fenced code block, in place of the prose it replaces. Do not add a diagram to a routine status comment.
- **HTML**: do not put HTML in a comment. When the human must explore many items or compare options, attach an `.html` report built from real data and name it in one bullet.
- **Supersession**: open with `Supersedes <8-char sha>` in the bullets and keep the prior evidence in Evidence; never edit or delete older comments.
- **Transport**: post multiline text through an argv list or stdin so the line breaks survive; fused words mean the newlines were lost. Read back one stored comment after changing how you post.
- **Check**: `scripts/kanban_comment_lint.py --board <slug> --since-hours 24` reports non-conforming comments by author. It is a report, not a gate.

## Outbound message formatting

- Use real line breaks, never a literal `\n`. Pass multiline text via an argv list, correct quoting or stdin/file.
- Never put backticks inside a double-quoted shell argument, because command substitution corrupts the record.
- Use short labeled sections with one copyable command per line. Read back the stored or delivered text and confirm the target chat.
