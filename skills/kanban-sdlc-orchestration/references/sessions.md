# Sessions, Continuity, and External Takeover

Read when a card spans several runs, when resuming a prior Hermes session, or when a standalone coding process must take over a card's worktree.

## Task continuity vs model-thread continuity

- Treat the card, branch/worktree, comments, attachments, exact-SHA evidence, and run summaries as the delivery identity; the model thread is disposable.
- Expect a fresh session after unblock, crash/timeout recovery, breaker retry, or a correction run.
- Never rely on hidden reasoning or a prior thread's memory; the next run cannot see it.
- End every run with a durable handoff (exact SHA, changed files, commands and results, artifacts, open risks, next action) so a fresh session can continue.
- Record decisions and scope changes on the card or approved plan, not only in chat; chat does not travel with the card.
- Run review in a separate role/session from the implementer; a shared thread breaks reviewer independence.
- A comment does not resume work; only a transition such as `unblock` makes the card runnable, and the next claim starts a new run.
- Default to same card + same worktree + fresh role/run session for dispatcher lifecycle runs.

## Codex App Server specifics

- One worker process owns one App Server subprocess and one thread, reused across turns of that run (including goal-loop continuations), then torn down on exit; do not expect it to survive a retry.
- Treat retry sessions (`Work kanban task <id> #2`) as separate attempts on the same card; inspect with `hermes kanban runs`.

## Resuming an operator investigation vs a dispatcher-owned worker

- Never interactively resume a dispatcher-owned session while its worker is active; two writers would share one checkout.
- Resume only a completed operator/diagnostic session the user asked to continue; it is not a substitute for a normal implementer retry.
- Resolve the exact session ID from session history, not from title or recency, because several sessions may discuss the same card.
- Re-read card, runs, process ownership, and worktree before resuming; never resume into an active worker's checkout.
- Give a self-contained bounded prompt: card, worktree, allowed side effects, required evidence, stopping condition.
- When driving the session programmatically, confirm the prompt was accepted (UI busy/running); a successful stdin write proves nothing.
- Distinguish running from completed; confirm outcomes on the board before reporting them.
- Never resume a session to carry reviewer hidden reasoning across role boundaries.

## External exclusive takeover

Covers a standalone coding process or a user-authorized resume of the prior worker session (e.g. after breaker exhaustion); not an automatic retry.

- Start only after verifying no native worker owns the worktree, using session IDs, PIDs, process ancestry, test/server children, and index locks, not cwd or command-line paths; a resumed CLI may show only `--resume <id>` and a repo-root cwd.
- Write the prompt outside the repo: card, worktree, branch, expected files (guidance), checks, prohibitions, final evidence; state that the operator owns claim, heartbeats, and lifecycle, so the child must not transition the card or signal its supervisor.
- Add a durable takeover comment, then run `hermes kanban unblock` and `hermes kanban claim --ttl <seconds>` back-to-back; if the claim loses to another worker, do not launch; observe the winner.
- Launch from the existing worktree with the original profile and a bounded budget (`hermes chat --resume <id> --query-file <prompt> --oneshot --max-turns <n> --run-budget <s>`); never run an open-ended watcher.
- Record PID, supervisor handle, session ID, TTL, and budget on the card; emit `hermes kanban heartbeat` while the child lives, keeping Kanban the lifecycle authority.
- Verify both sides immediately: the process reports the intended session and the card shows the claimed run `running`; either alone is not valid recovery.
- Allow no concurrent worker or diagnostic in the worktree; overlapping evidence cannot satisfy a gate.
- The takeover may commit and open PRs but never bypasses inspection, review, or merge gates, and never force-pushes.
- After exit, read back the whole platform-visible PR (head, commits, files, merge base vs live target); a clean local diff can hide contaminated ancestors. Route contamination to the same implementer, no duplicate PR.

## Supervisor or child killed by signal

- Treat every side effect as state unknown when the process or its supervisor exits by signal or nonzero after writes; the exit code proves neither success nor failure.
- Stop heartbeats, then reconcile before relaunching or re-pushing: surviving descendants, index locks, card/run, status/reflog, local/remote/PR heads, session transcript.
- Verify an asynchronously started push against both the remote ref and the PR head before retrying it.
- Preserve valid partial commits and resume only the unfinished lifecycle steps.
- Never kill by stale PID, profile name, or cwd alone; follow recovery.md when process ownership is ambiguous.
