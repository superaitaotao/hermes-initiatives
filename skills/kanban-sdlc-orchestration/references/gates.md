# Gates: verification, CI, QA, and human inspection

Read when a card needs local verification, exact-HEAD CI, a CI repair card, frontend visual evidence, or a human inspection approval or rejection.

## Local verification

- Run the project-prescribed focused tests plus relevant surrounding checks before handoff, because CI is not a substitute for local evidence.
- For bug fixes, write the regression test first and confirm it fails for the expected reason, because a test that never failed proves nothing.
- Never weaken, skip, or delete a failing test to advance a card, because that fakes the gate.

## Exact-HEAD PR CI

- Bind CI evidence to the exact PR head SHA, because a green run on another head says nothing about the candidate.
- Route a CI failure back to implementation on the same card, branch, and PR: fix, rerun local verification, push, and wait for the new exact-HEAD run.
- Judge verdicts per attempt, not per run id, because a rerun of the same run id can supersede a failed attempt. Re-read run status, conclusion, attempt, and jobs (`gh run view <id> --json status,conclusion,attempt,jobs`) before opening a repair card, re-scoping a card, or reporting a fix.
- Treat `cancelled` jobs or shards as unverified, never as passed or failed, and state that limitation in the card and report.
- On a failed-jobs-only rerun, verify the previously failed shard and the aggregate report yourself, because reused jobs can hide it and downstream promotion may stay skipped, not deployed.
- Keep the failed attempt as history. A passing newer attempt clears only that exact-SHA failure; it does not prove the root cause or validate a later target-branch head.
- If the authorized response was a no-code rerun, do not invent a source fix or manufacture a PR to satisfy a repository completion contract. Record the evidence, keep the card non-runnable, and report a lifecycle-capability blocker rather than spawning another implementer or claiming done.
- Do not use `archive` as a bookkeeping shortcut for a worktree card, because it may remove the worktree; see queue-and-archive.md.
- Delivery cards gate only on their exact PR head and required PR checks, not on post-merge full-integration main CI.

## Queued CI and runner capacity

1. Read the candidate PR and exact-head run before classifying a CI block:
   - `gh pr view <pr> --repo <repo> --json state,headRefOid,mergeStateStatus,statusCheckRollup`
   - `gh run view <run> --repo <repo> --json status,conclusion,headSha,jobs`
2. Inspect runner occupancy with `gh api repos/<repo>/actions/runners`. Read the alleged holder's job matrix to confirm it still runs; online-and-busy flags alone do not identify its work.
3. Treat queued checks and their waiting prerequisites as unverified, not failed. An empty conclusion does not authorize a repair, rerun, cancellation or review handoff.
4. Leave legitimate capacity waits non-runnable under their existing resume contract. Do not cancel another delivery's browser jobs, change workflow routing, or repeatedly unblock the waiter.
5. Once the required checks pass, re-read card ownership, PR head and conflict state. Resume through the supported same-card route only when all pre-review gates pass; retain pending receipts as history and append the final CI result.

## CI repair cards

- Give a CI repair lane blocking a PR, `main`, promotion, or release higher priority than routine work (value in the project policy reference), and read the priority back. Priority reorders eligible cards only; it never bypasses gates, interrupts an owner, or justifies a duplicate card.
- Open an exact-main repair card only after the run is terminal, because an in-flight run has no stable verdict.
- Use the idempotency key `<repo-slug>-ci-<full-sha>-<run-id>`. Search the board by key, SHA, and run id first; never reuse a card scoped to another SHA or run, and never reopen one for a later run.
- Build the body from the run itself: full SHA, run URL and id, failing job names with job ids and durations, failing test titles from the job-log summary, the failure mode (timeout vs assertion), unverified cancelled shards, and which other cards it does not supersede.
- Assign the implementer lane and use a repository completion contract so completion requires a published PR.
- Post-merge exact-main CI failures get their own SHA-scoped repair card or dedicated CI workflow. They never block, hold, or reopen an already-merged delivery card whose PR checks, scope, and target-branch reachability are verified.

## Mass browser failures

- Before treating a mass browser-suite failure as many regressions, diff the environment against the last green run: deployed revision, origin the shards target, CI variables, and front-door health, because a wall of "element not found" or empty bodies is usually one entry-point defect.
- Fix it durably in the suite: fail-fast front-door preflight plus shard targets resolved from deployed config.

## Frontend QA and visual evidence

- Use repository-owned headless browser tooling; start services through repository wrappers and read the worktree's actual ports.
- Exercise the changed path and a control path, recording console errors, page errors, and failed network requests (or explicit zero counts).
- For non-trivial frontend candidates, attach exact-SHA labeled desktop and mobile screenshots to the same card, plus videos when motion, interaction, or a multi-step flow changes.
- For non-trivial frontend, the independent reviewer re-verifies the live exact-head candidate itself with `computer_use` in real desktop and mobile-sized browser windows and attaches its own captures, because implementer attachments can be stale, mislabeled, or from the wrong server. It blocks rather than approves when `computer_use` is unavailable or either viewport does not show the change.
- Decode and inspect the stored card copy of every screenshot and video at full resolution, because a valid header or upload receipt does not prove a usable image.
- Keep an unusable attachment as history and send evidence-only rework to the same implementer, card, and PR, without touching production code or implying approval.
- Prove which revision a reused or already-running server serves before trusting its results, because a reuse-existing-server runner silently tests whatever is running, often the shared checkout.
- For deferred resources, prove the request count is zero before the triggering action and positive after.
- Keep capture scripts and screenshots out of tracked scope (check `git status`), so evidence does not contaminate the candidate.
- If the host browser crashes on both candidate and control, keep that evidence and rerun the exact pinned test-runner version on a supported renderer (for example the matching official container image) with the SHA, services, workers, and retries unchanged. Record renderer, image digest, command, and test count. The run counts as evidence only if the suite actually executed.
- A supported-renderer pass clears an execution blocker, not visible findings. Classify any finding against PR scope and control, keep it visible for the human decision, and never fix product behavior opportunistically in an unrelated PR.

## Human inspection

- When a card needs human frontend verification, start the candidate's services yourself before blocking, using the repository wrapper inside the candidate worktree, and confirm both answer on that worktree's ports.
- Prove the link serves the candidate worktree, not the shared checkout, by checking the serving process's working directory.
- Give a link the user's remote client can open (resolved hostname, not a raw private IP) over HTTPS, because a `Secure` session cookie is silently dropped over plain http and authenticated paths become unreachable.
- Fix the origin, not shared config. Serve the candidate over a private HTTPS listener instead of flipping a shared environment flag that affects every consumer.
- Prove a real login round-trip on the origin the browser will use, checking the cookie flags and an authenticated response body, not just a `200`.
- Post the exact SHA, links, concrete steps, and expected result on the same card, confirm the non-trivial desktop/mobile evidence is already attached, and block with a precise `needs_input` reason.
- Leave services running until inspection or merge resolves; a listener `502` means the upstream stopped, so restart via the wrapper and report it.

## Inspection approval

- Bind a terse "approve" only to the uniquely enumerated gate, card, and SHA in the immediately preceding operator report; otherwise ask one scoped question.
- If replacement evidence arrived after an earlier decision, ask for a fresh visual verdict, because approval cannot cover artifacts the user has not seen.
- Re-read the card, runs, candidate/PR identity, merge state, and every other prerequisite before mutating, because the report the user answered may be stale.
- Record approval as a user-authored decision on the exact SHA (`hermes kanban comment <id> "..." --author user`). It satisfies only inspection and never stands in for CI, deployment, merge readback, or review.
- If inspection was the final prerequisite, `unblock --reason "..."` and then immediately `request-review --reviewer <reviewer> --summary "..."` in the same operator process, because `request-review` requires `running` or `ready` and a card left `ready` under the implementer respawns or hits `respawn_guarded: active_pr`. Verify a fresh reviewer claim or run before reporting review as started.
- If another prerequisite is pending, keep the card non-runnable (normally `scheduled`), record the remaining predicate, and read the comment back. When the predicate succeeds, re-read and route straight to review without asking for approval again. If it fails, route it by that gate's own rules. A watcher timeout is not a failure result.
- If the candidate merged before inspection or review finished, never reopen implementation. Verify that PR head equals the approved SHA, reconstruct the merged delta, confirm target-branch reachability, record approval against the pre-merge SHA, and route the same card to independent post-merge review. Post-merge main CI is not a gate (see review-and-merge.md).
- Approval covers only the inspected SHA. Any replacement head needs fresh inspection, and the old approval stays as stale history.

## Inspection rejection

- Treat a reported defect as a lifecycle rejection. The operator never implements, commits, pushes, or runs replacement gates unless an explicit takeover (exclusive worktree, bounded claim/TTL, heartbeats, no concurrent worker) is recorded on the card.
- Re-read the card, assignee, worktree, branch, SHA, PR, and runs, and reuse the existing card and graph. Never create a duplicate card for a correction.
- Record a structured finding on the same card with `Candidate` (full SHA), `Observed`, `Expected`, `Reproduction`, `Evidence`, and `Required action`.
- Mark approval, QA, and CI for the rejected SHA as stale for any replacement head. Invalidate them, never delete them.
- Route rework to the original implementer on the same card, branch, and PR. Use `request-changes <id> <reason>` for an active review run with valid provenance. For a blocked `needs_input` inspection, use it only after confirming (via help plus a focused lifecycle test) that the installed Hermes supports source-aware manual-inspection rework. Otherwise keep the card blocked and record the capability blocker. Never apply this path to auth, transient, dependency, or circuit-breaker blocks, or on stale `needs_input` provenance.
- Verify a new `claimed` event and `running` status before saying work resumed, because `ready` is only queued and a comment is not a transition.
- On `respawn_guarded: active_pr`, report the card as queued/guarded. Do not delete or rewrite PR or history to evade the guard, and do not implement in chat. Record the blocker and get a supported recovery or owner decision (see recovery.md).
- Label any operator-produced commit as external input that the implementer must audit and adopt. Then gates resume in order: local verification, QA evidence, push/CI, fresh inspection, independent review, merge.
