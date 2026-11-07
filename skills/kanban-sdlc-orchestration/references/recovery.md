# Recovery

Read this when a card is blocked, crashed, timed out, looping, held by a guard or breaker, or when someone asks why automatic recovery did not happen. Each section names a failure class, how to recognize it, and the bounded response.

## Principles

- Classify the failure before any reset, because a reset without a named failure class repeats the same failure.
- Re-read the board, card, recent runs, latest worker log, and live process/heartbeat immediately before every mutation, because the dispatcher and auto-decomposer act between your commands.
- If a run is `claimed` or `running`, make no lifecycle mutation and create no follow-up card.
- Allow one deliberate reset per lane, with a recorded, materially changed strategy. If the reset fails the same way, the strategy is disproven: restructure, transfer to a capable lane, or escalate to a human, because repeated `unblock` turns a bounded breaker into an unbounded loop.
- Recover proactively when a structured blocker has a machine-verifiable predicate (dependency merged, exact-HEAD CI green, defect fixed on the target branch) and readback proves it cleared: unblock with `--reason`, then verify a fresh claim.
- Preserve the card, branch, worktree, comments, and failed runs. Never relabel failures or duplicate the graph to escape a limit.
- Recovery changes mechanics, not policy: every gate still applies to the new head (see `gates.md`), and old evidence stays as stale history.
- Fail closed: when ownership, identity, authority, or state is ambiguous, leave the card blocked and report.
- Use a supported surface (CLI, dashboard verb, worker tool) for every transition. Where none exists, the change needs explicit user authority, the board dispatch lock, and an appended audit event; never edit Kanban storage silently.

## Why automatic recovery stopped

- Name the stopping mechanism before per-card detail, because recovery is designed to stop on repeat and a stalled board usually means the inputs are still broken:
  - **Sticky block:** an explicit block, or a `gave_up` whose payload carries `sticky`. It leaves `blocked` only through `unblock`, `promote`, or the dashboard's Blocked → Ready.
  - **Failure limit:** a plain `gave_up`, held by the failure counter.
  - **Recurrence limit:** `block_loop_detected` routes repeats to `triage`.
  - **Respawn-guard hold:** the dispatcher refuses an otherwise eligible card.
- Count each card's event kinds to identify which mechanism fired, and release only after the underlying defect is fixed.
- The dispatcher returns a non-sticky `blocked` card to its source phase once its parents are done. Nothing expires `scheduled` on a timer.
- Operator verbs that reclaim or change the assignee (`reclaim`, `assign`/`reassign` to another profile, an assignee round-trip) reset the failure counter. Count any of them as the card's one deliberate reset.
- Audit scheduled jobs separately: which may transition lifecycle versus only alert, whether each actually runs (enabled, failure streak, last success), and which cards fall within its mandate. A job that prints an `unblock` command is advice, not automation.
- Do not call standing automatic recovery active without a durable user-approved policy listing allowed machine recoveries and forbidden human gates, plus a read-back scheduler-owned run and same-card transition.

## Timeouts and retries

- Treat "timed out; dispatcher will retry" as an observation, not an unblock request, because the retry is often claimed on the next cycle and an eager `unblock` races it.
- Confirm with `show` and `dispatch --dry-run`: a card at its retry cap stays `blocked` even when the notification still promises a retry.
- Inspect the checkpoint (`git -C <workspace> status --short`, `log -2`, the machine-readable test report). Verify report scope, failures, skips, and source identity before calling a gate passed, because a clean tree or passing report does not bind the run to the current HEAD. Record missing provenance as unverified.
- If prior runs left verified progress, comment the checkpoint SHA and remaining steps; one `unblock --reason` then resumes from it. If preflight finds a fresh running retry, stop and verify it instead of reclaiming.
- If only attachment, publication, or closeout failed, recover the existing artifacts without rebuilding (`evidence-transport.md`).

## Iteration-budget exhaustion

- Diagnose cumulatively: runs that hit the cap, total iterations and elapsed time, whether a retry is live, and the dominant waste pattern. Iterations are internal agent-loop cycles, not user messages; never call repeated full-cap runs normal progress.
- Rule out a refusal loop first. Calls that return far faster than the operation could finish are refusals, not executions. If most calls were refused, the defect is lane-wide: fix the lane's approval or tool contract, not the card. Separate the classes from the lane's own records: a growing approval-block count means approval policy; an unchanged count means another layer (tool-parameter contract, schema validation) rejected the calls, so read the transcript before changing approval config.
- Common causes: oversized card, rediscovery, serial broad reads, repeating a rejected call, drift after green.
- Shrink the execution shape instead of raising the cap: bounded checkpoints inside the existing graph, each naming exact files, one deliverable, a bounded verification set, and a durable handoff.
- When code is green but uncommitted, hand off a checkpoint resume: commit, push, open the PR, request review, no re-exploration.
- The cap is profile-wide; raise it only with explicit user authority, recording the prior value.

## Goal-mode judges

- A goal-mode judge is an extra gate with its own bar, not the card's contract. A judge that demands the merged end state refuses the review handoff that policy requires first.
- Signature: a green, mergeable head parked in `needs_input` on a judge rejection. Do not reset or re-dispatch it.
- Route the candidate to review through a surface that does not run the judge (the dashboard's review verb, via `scripts/kanban_triage_route.py`). The reviewer's own completion still faces the judge, which the merged end state satisfies. Record the judge bar as the blocker.
- Goal mode has no supported edit surface, so disabling it falls under the no-supported-surface principle above.

## Disconfirmed repair entry gates

1. Read the approved plan's runtime predicate and stop rule before changing source. Separate toolchain failures from application failures, because an environment timeout does not establish the planned defect.
2. When unchanged source passes and the predicate is absent, preserve the evidence. Do not manufacture the expected failure or silently substitute another repair.
3. If the contract names re-planning, route the existing card to the planning/reconciliation lane through supported transitions; that is resolved input, not a new user decision.
4. Keep implementation stopped until the revised plan passes independent approval, reusing the existing card and graph.

## Human-choice and wall-clock waits

- Once a worker has produced a concrete human choice, stop re-specification: keep the card non-runnable, present the decision once with a recommendation, and wait, because a respawn rediscovers the same blocker.
- Park cards waiting on a human decision or wall clock in `scheduled` (`hermes kanban schedule <id> <predicate>`), not with repeated `needs_input` blocks, which trip `block_loop_detected` into `triage` churn. Workers cannot park their own card.
- `schedule` does not accept `triage`. Route the card out of `triage` first and schedule it under the same dispatch lock, then read back `scheduled` with an empty claim, because a run spawned between the two steps defeats the park.
- Before parking, record the resume contract: frozen head, run id, and separate success/failure next actions.
- When the harness's own gate is the blocker, do not wait on repair cards stuck in the same loop; route the fix directly under user authority.

## Lane capability defects

- A seconds-long `capability` block or instant crash is a lane defect, not a card defect. Do not unblock and re-dispatch the same incapable lane.
- Replay a "missing tool or dependency" claim with the project's canonical runner and an explicit interpreter before accepting it, because a failed bare probe shows only which environment the lane defaults to.
- Resolve the failed worker's effective route first: card-level model/provider overrides, then lane configuration. Probe that exact route with one completed model turn, because success on another route, or a model-list request, does not prove completion works on this one. Print only sanitized status; never dump credentials or auth headers.
- Read a dead route's own response before blaming its config. An instant rejection with a retry-after hint while an identically configured sibling succeeds is runtime cooldown or quota, not a structural defect: report the expiry and the cheapest remedy.
- Prefer fixing at the card: a task-level `set-model <card> <model> --provider <p>` override to an equivalent route, with a durable comment. A different model family changes what the lane runs and belongs in the approval record. Avoid shared-config edits and router restarts while workers are active.
- Before handing a repaired card to another role, clear repair-only overrides so the next role uses its own lane.
- Keep implementer and reviewer models distinct, because shared models weaken review independence.
- Granting a lane new toolsets or approval levers is a capability grant and needs explicit authority.
- A worker that heartbeats but makes no tool calls may be in provider retry backoff; heartbeats prove liveness, not progress. Fix the route, prove it with a one-shot turn, then `reclaim` and verify the new run makes tool calls. Waiting or unblocking only repeats the backoff. When the max-runtime sweep is close, let it reap the run instead, because a manual `reclaim` resets the failure counter.
- For repeated same-kind capability blocks, preserve the run and record the replacement lane and safeguards on the existing card before transfer.

## Review-lane provenance

- `reclaim` and the max-runtime sweep return a card to the phase its run was claimed from, so a reclaimed reviewer stays in `review`. Never follow it with `reopen-review`, which sends an unreviewed card back to the implementer.
- `request-changes` routes to the implementer recorded by the newest `review_requested` event. If a reviewer's return is refused for missing provenance or a non-review claim, the verdict may still be sound: repair the lifecycle, not the review. Cite the verdict's evidence, park the card, and reassign it to the recorded implementer under the dispatch lock, then read back status and assignee.
- Prevent provenance loss at the handoff: the card must be assigned to its implementer when review is requested, and the recorded implementer must read back non-null.

## Respawn-guard holds

- Read the guard reason in `dispatch --dry-run --json` (`respawn_guarded`) before acting. Guards suppress spawns silently, including `review` handoffs.
- **Stale blocker text:** auth/quota guards scan stored failure text, which may quote unrelated worker output or a superseded run. Replay the installed pattern read-only on the stored text and check whether any real cooldown elapsed. Clear the field, not the evidence: `unblock` on `blocked`/`scheduled`; on `review`, an assignee round-trip to the same profile, counted as the card's reset.
- **Open-PR hold (`active_pr`):** holds the ready lane, not review, while a recent comment holds a full PR URL, until a real handoff follows it (a change of assignee, `changes_requested`, or `review_reopened`). A same-profile reassign is not a handoff.
  - If every pre-review gate is complete, hand the card to `review`.
  - If required publication remains, apply the existing bounded-owner decision once; route to ownership reconciliation only when that decision is missing or ambiguous.
  - When the owner is unambiguous and already assigned, leave the card `ready` and report it as machine-held. Do not promise a release time, because installs may carry local release exceptions; read the project policy before forecasting.
  - Do not round-trip the assignee merely to lift the hold, because the assignment event can restart an unintended implementer.
- Keep full PR URLs out of receipts posted after a handoff (use `PR #<n>` and the SHA), because a later URL comment re-arms the hold.
- A worker's informational comment is not a handoff and does not move any hold.
- After clearing, verify `respawn_guarded` no longer lists the card and a fresh run heartbeats. A real `dispatch` reporting zero spawned usually means the gateway dispatcher already claimed it.

## Process identity and competing writers

- Run terminality and process exit are separate facts; a terminal run may still have a live process finishing closeout.
- Never signal by stale PID, profile name, or cwd alone. Verify exact PID, start time, command, descendants, cgroup/scope, and current claim; allow one bounded grace window; signal only the process that provably belongs to the terminal run, with authority; verify it and its descendants are gone. Enumerate every child writer (installers, servers, test runners) before handing a worktree to another role.
- A worker-launched process can escape the worker's scope (its own service scope, a shared browser or daemon) and still hold the workspace. Prove ownership from cwd and start time against the ended run, report it, and signal nothing another session may share without current approval.
- For an external or resumed-session takeover: comment, then `unblock` and `claim --ttl` back-to-back; if another worker wins, do not launch (`sessions.md`).
- Evidence produced during overlapping ownership is contaminated and cannot satisfy a gate.
- After any signal or nonzero exit, treat side effects as unknown: reconcile worktree, index, reflog, local/remote/PR heads, and transcript before relaunching or pushing again.
- Read back the full platform-visible PR scope and merge base against the live target; block before review if contaminated. Never force-push or open a duplicate PR.

## Resource pressure and OOM

- Do not classify a vanished worker from `pid not alive` alone; correlate kernel/service OOM records, cgroup peak versus limit, surviving children, and app-server health.
- Treat a clean exit without a terminal Kanban call as a suspected OOM until scope evidence rules it out; read the crash event's exit fields before narrating a cause.
- A child OOM can stop the whole worker. Under systemd's default `OOMPolicy=stop`, one kernel kill inside a transient worker scope makes systemd stop the scope and SIGTERM every process in it, including the agent. The agent then ends its turn as interrupted and exits 0, so the run looks like a protocol violation, not an OOM. Confirm by matching the scope's `Failed with result 'oom-kill'` time with the tool's SIGTERM exit and the turn-end time. A worker scope created with `OOMPolicy=continue` (systemd 253 or later) survives instead and sees the child's exit 137 as a failed command; read the scope's properties before you assume either behaviour.
- Measure a gate's peak before you call it in-worker sized. If test modules import the app at collection time, every run pays the app's full startup data load before the first test, so a one-file run fits and the full suite does not. Compare the `--collect-only` peak with a single-file run.
- A cgroup created *below* the worker (a nested cgroup, `ulimit`, a child's own memory flag) inherits the worker's ceiling, so it is not a remedy. A sibling unit that the user's service manager starts (`systemd-run --user --scope` or `--wait --pipe`, even when called from inside the worker) lands outside the worker's cgroup with its own bound: that is how a heavy gate (production build, full test suite) runs from a worker without being killed. Size the bound from the gate's measured peak. Without such a manager, delegate the heavy gate to exact-HEAD CI or an adequately provisioned runner. Never bypass a hook to make it fit.
- A dev server with an on-demand compiler (`next dev`) cannot be held to a fixed memory bound: it keeps every page it has compiled, so its peak grows with the number of pages visited, not with load. A capped unit running one, with restart-on-failure, OOM-loops. Serve a production build under the bound, and measure the dev mode separately if anything still needs it.
- Check what the test runner starts itself, because a runner that launches its own app stack doubles the footprint.
- Classify evidence in time order: earlier gates stay valid for their SHA, the first genuine assertion is the finding, and failures after the app died are fallout.
- A breaker made of OOM strikes has an environment cause; keep it closed until the execution shape changes, and check other cards for the same pattern.

## Gateway restarts and dispatcher authority

- Treat gateway restart, setup, or update as destructive to running workers: inspect running cards first and wait, requeue, or accept interruption; attribute later `pid not alive` diagnostics to the restart.
- `systemctl --user reload` on the gateway is a restart, not a graceful reload: read the unit's `ExecReload` (`systemctl --user show <unit> -p ExecReload`) before trusting any "graceful" claim. A signal-based reload drains the runtime, so live workers fail with `runtime_draining` and their retries burn the failure limit within minutes, which blocks every running card.
- **Changing dispatcher config (for example `max_in_progress`):** edit every config the dispatching gateway reads, plus any profile that mirrors the value, and validate it with a YAML load. The dispatcher reads `kanban.*` at boot, so the change is staged until the gateway restarts. Count `running` cards before activating. If any card is running, report the change as staged and ask the user whether to restart now and accept the interruptions, or to wait until the board drains. After a restart, read back the gateway PID/restart count, then route each card that crashed with `runtime_draining` back to its interrupted phase as an environment-transient failure that uses no reset budget.
- Before any Kanban config write, identify the physical board database and every gateway that sweeps it, deriving each gateway's home from process evidence; the shell's profile proves nothing.
- Keep exactly one dispatcher per physical board, and prove dispatch from that gateway plus a recent claim, not from lane unit states.
- Report config as staged, loader-validated, or activated (restart/reload proven). If an in-gateway guard refuses self-restart, keep the config staged and ask for an external restart once the board is safe.

## Global dispatch pause (emergency stop)

- **Recognise it:** `ready` and `review` cards sit unclaimed for a long window while `dispatch --dry-run` reports zero in every bucket and nothing on the cards is failing. The gateway log records dispatch paused by a global emergency stop with a reason.
- Only `claimed`/`running` is started, so a paused dispatcher idles the whole board without breaking any card; the pause stops new work and never kills in-flight workers.
- An install update engages the pause itself and lifts it when it finishes, so the wait can span most of the update window. Correlate the pause timestamp and reason with the running update or operator action before concluding the board is stuck.
- Do not reset, reopen, re-request review, or resubmit anything. If the owning activity is still in progress, wait and confirm the next tick admits a worker.
- If the activity has finished and the sentinel is still present, lift it once with `hermes resume` and verify a fresh claim. A sentinel that reappears with no new pause action is an updater defect to report, not something to clear repeatedly.

## Gateway/worker code skew after an install update

- **Recognise it:** cards on several lanes crash seconds after spawn, before any model call, all with the same refusal at worker admission or identity verification. The first crash follows an install update (the checkout's `git reflog` pull time, changed file mtimes) while the gateway's `MainPID`/`ActiveEnterTimestamp` predates it. Sibling services may log that they retired themselves on version skew while the gateway only warned.
- **Mechanism:** each spawned worker imports the new code from disk, but the long-running gateway keeps the modules it booted with. Any changed handshake (identity fields, units, payload schema) then mismatches on every spawn.
- **"The gateway is up" is not health.** Compare the gateway start time with the time the install HEAD changed. Also read `readlink /proc/<MainPID>/exe`: a path ending in `(deleted)` means the gateway still runs an interpreter that a runtime update replaced, so it needs a restart through its unit regardless of code SHA. A running gateway on stale modules blocks the board as surely as a dead one.
- **An update that "restarted the gateway" may not have replaced the process.** On a multiplexing host gateway, a restart addressed to a served (non-default) profile only unserves and re-serves that profile's adapters inside the same process; it exits 0, so the caller reports success. UI updaters that restart the gateway "for the active profile" hit this whenever the UI's current profile is not the host profile. After any update-triggered restart, verify the host `MainPID` changed; matching `[MULTIPLEX] Profile '<p>' unserved` / `Now serving` log lines with no process restart are the signature.
- **Trace who pulled, so the right updater gets fixed.** Correlate the reflog pull time with the user journal (`journalctl --user --since … --until …`): an update-apply request in a UI service's access log, a scheduled update job, or a CLI update log entry. Then name the defect in that updater, not just "the gateway was not restarted".
- **Prove it before you report it.** Do not stop at "likely cause, not proven" when a short probe settles it, because the user will ask for the real mechanism:
  1. Read the traceback's `file:line` from the lane's `errors.log`.
  2. Find the SHA the gateway loaded (skew warnings, update logs, reflog entry before the pull) and run `git diff <loaded-sha> HEAD -- <files in the traceback>`.
  3. Run the old and new versions of the changed function on the same live process in the Hermes venv and show the values differ.
- **Recovery cannot clear it.** The blocker predicate never becomes true, so automatic recovery correctly does nothing. Every newly dispatched card spends its failure limit and blocks. Count the growth from the dispatcher's per-tick `crashed=` lines and report that the damage grows until the restart.
- **Audit the update path before proposing the restart.** The user expects the updater checked first, because a restart alone clears the symptom and the next update repeats it. Read every updater that touches the install: the scheduled update unit and its script, plus any UI "update agent" action. Look for three gaps:
  - The updater exits on "no update available" without checking whether the gateway runs stale code.
  - Its post-update check only confirms the services are active, without confirming the gateway's loaded SHA matches HEAD.
  - Two updaters follow different channels and fight over the checkout.
  Offer to fix the updater and let it perform the restart through its own pause → drain → restart → resume path. Hermes-specific checks are in the `hermes-agent` skill, `references/troubleshooting.md` → "Auditing an update path".
- **Response:** a gateway restart is the only fix. Apply the restart rules above (count running cards first; user authority). Say that the restart also loads any uncommitted local edits in the installed checkout. After the restart, route each skew-crashed card back to its interrupted phase as an environment-transient failure that uses no reset budget, and verify fresh claims. Report the missing guard as a Hermes defect: an update must restart the gateway, or the gateway must refuse spawns while its loaded code differs from the install.

## Worker bootstrap failures

- An import or module error raised before the worker acknowledged ownership is a runner environment defect, because no job code ran. Repair the managed environment instead of editing prompts or scripts.
- **Workspace shadowing (a module-not-found for the worker module on one card or one repo's cards only):** the dispatcher starts the worker as `python -m hermes_cli.<worker module>` with the card's workspace as cwd. `-m` puts cwd first on `sys.path`, so a workspace that is itself a Hermes checkout (fork cards) imports *its* `hermes_cli/` instead of the installed one. If that branch lacks the worker module, every run dies instantly; if it has it, the worker silently runs branch code. Every card whose workspace is a Hermes checkout is exposed, including queued `todo` ones.
  - Prove it in three runs with the gateway's interpreter and the worker's `PYTHONPATH`: from the workspace (fails), from a neutral cwd (passes the import), and from the workspace with `python -P` (passes). Then confirm `git -C <workspace> ls-tree HEAD hermes_cli/<module>.py` is empty while the installed checkout tracks it; `git log --full-history -m --diff-filter=AD -- <path>` finds the commit that removed it on the branch's base.
  - Get the interpreter from the gateway process, not the venv: `readlink /proc/<gateway MainPID>/exe`. A different interpreter can mask the failure because its `sys.path` order differs.
  - Fix the launch, not the clone: run the worker with `-P` or `PYTHONSAFEPATH=1` in the dispatcher's worker argv/env, with a test that spawns from a workspace containing a decoy `hermes_cli/`. Rebasing the clone onto a base that has the module only hides the shadowing. This is a local Hermes patch, so it lands on the fork's local-patches line and needs a gateway restart through its unit.
  - Recovery correctly refuses to retry it, and the card's `BLOCKED — MACHINE` comment usually names a "runtime owner" that no card or job is. Report it as an orphaned machine block and offer one repair card that is not a child of the blocked card (`Operational cards that expose defects`).

## Unattended reviewer lanes

- Enable the tool-loop hard stop on each unattended reviewer lane so a looping reviewer stops instead of burning its budget. This is lane setup, not per-card recovery.

## Protected-file consent gate

- Expired or undelivered consent prompts for protected instruction files are handled in `planning.md` → "One-operation protected edits".

## Gate conflicts with retained operational artifacts

1. Replay the gate read-only with the same configuration loading as its hook; a summary alone does not establish the cause.
2. Read the artifact-producing operation's approved contract. Deleting required backups or receipts to clear a gate violates that contract.
3. Find, or create through the planning/reconciliation lane, one gate-repair owner. It is not a replacement delivery card.
4. Specify a narrow compatibility rule (artifact type, environment, exact name format) so unknown artifacts and real drift keep failing, and make every exempted artifact visible in output.
5. Require failing-then-passing tests and a real gate replay. The affected delivery waits until the repair merges; its implementer then integrates it and re-verifies the new head.

## Operational cards that expose defects

- Do not widen a no-source-edit operational card into implementation. Block it, reuse or (with authority) create one repair card that is not its child, because an open parent would gate it, and keep the operation non-runnable until the repair merges.

## Circuit breaker and late auto-decomposition

- After a breaker routes a card to `triage`, re-read before every mutation, because auto-decomposition may have added children and dependencies since.
- If `promote` reports unexpected unsatisfied parents, stop and inspect; never force-promote from a stale read.
- Reuse a valid auto-generated graph. If late fan-out duplicates work the canonical card owns, reclaim spawned children before they mutate, archive only confirmed-redundant ones, unlink only auto-added blocking edges, and preserve every event.

## Moving a card out of `triage`

- `triage` has no CLI exit that preserves the card (`unblock` takes blocked/scheduled, `promote` takes todo/blocked, `specify` rewrites the title and body). Use the dashboard's status verbs through `scripts/kanban_triage_route.py --board <slug> --plan <plan.json>`, `--dry-run` first; never repoint or reuse a shared dashboard server.
- The script owns an isolated loopback dashboard, refuses cards with a live claim or task-owned processes, holds the board dispatch lock across each transition, and comments only after readback. Do not hand-roll it, and do not use `hermes pause`, because a profile-level pause does not stop the gateway dispatcher and a root pause stops the whole fleet.
- `review` accepts only `ready`/`running`, so the route pairs `ready`→`review` under the lock. A PR-backed card needs `published_pr`; a `local-only` card declares its contract and no PR. Pass `implementer` when the card is assigned to its reviewer, so review provenance is correct.
- A `blocked` card whose newest block carries `source_status: review`, or a breaker `gave_up` whose crash payloads carry `retry_status: review`, returns to `review` on plain `unblock --reason`, keeping its reviewer assignee. Do not route it through the script: its first step (`ready` plus the implementer assignee) already resumes the card into `review` assigned to the implementer, and its second step then fails with HTTP 409. If that happens, read the card back and reassign it to the reviewer before the next dispatcher tick, because otherwise the implementer lane runs its own review. A `scheduled` card always unblocks to `ready`.
- A dead reviewer run can leave its preview servers running in the card's worktree. The route refuses such a card for task-owned processes. Prove the processes belong to the ended run (cwd, start time), release them with the project's own server-release command, and then route the card.
- Land the card in the phase its remaining gates justify. An open PR alone never authorizes review; finish mandatory pre-review publication first.
- Write `RECOVERED` only after readback shows the new status. A `ready`/`review` card is queued, not started.
