# Review, Merge, and Closeout

Read this when reviewing a candidate, reacting to lifecycle notifications, routing review/rework, recovering a conflicted PR, merging, or closing a PR-backed card.

## Independent review

- Review the actual diff against acceptance criteria and rerun relevant checks, because implementer claims are not evidence.
- Take exactly one action: approve and continue to the merge gate; request actionable changes (returns to the original implementer); or block for a genuine external/human prerequisite. Ordinary feedback is never a generic blocker.
- Bind every verdict to the exact candidate SHA; a replacement head makes prior CI, QA, manual approval, and review stale (kept as history, never gating).
- Keep authority-bearing facts (reviewed head, merge method, verdict) in a durable reviewer-owned record, because implementer completion metadata cannot prove independent review.
- For leased/cross-process lifecycles, review the transition matrix (slow startup past a lease, ownership loss mid side effect, failure during drain, slow-poller shutdown), not just the happy path; a lease held only for drain is not an active owner.

## Interpreting lifecycle notifications

- Treat notifications as possibly superseded observations; re-read the card, runs/events, parents, children, and live workers immediately before deciding or mutating.
- Make no transition when the same-card lifecycle has already advanced; report the live run instead.
- A synthetic `review_requested` created only to restore provenance is bookkeeping, not a new candidate; report the live reviewer run rather than re-reviewing it.
- A completed review with `review_outcome=changes_required` is a finished rejection, not approval; verify the canonical card is claimed with those findings.
- Classify a `changes_requested` verdict by the leading token of its reason text, not by the notification's verdict word or the event kind. The event always returns the card to the recorded implementer; only the authority it carries varies, so a reason opening with an approval token (`APPROVED`, `PLAN APPROVED`, `MATRIX APPROVED <digest>`) authorizes the next phase even while the notice reads "implementation is not approved", and a reason opening with a changes token withholds it. Read the reason before reporting a card as blocked or asking the user to decide, and never re-route such a card to the reviewer.
- A completed routing/orchestrator card means routing finished, not implementation; read back the child, edges, and gates. `ready` is queued; only `claimed`/`running` is started.
- Call a worker active only after confirming current run, process identity, and recent heartbeat; a quiet log is not failure evidence.
- Create no follow-up card until the live graph proves no existing path covers the work.
- When asked why auto-completion did not occur, name the causal gate (exact live run or unmet predicate) first, then the bounded recovery. `exit_code=0` means only the process returned; a missing terminal transition is still a protocol failure.

## Routing review and rework

- Issue `hermes kanban request-review <id> --reviewer <profile> --summary ... --metadata ...` only from an implementer-owned `running` or `ready` state.
- Assign the card back to the original implementer before any `request-review`, because a handoff issued while assigned to the reviewer records the reviewer as implementer and misroutes later changes.
- Recover by event order and provenance, not the latest label: a late `timed_out`/`gave_up` after a valid `review_requested` can overwrite `review`; restore the review lane without changing implementation ownership.
- On a dispatched board, never send the `ready` assignment and the `review` handoff as separate commands, because the dispatcher claims `ready` in between. Do both (plus any goal-mode disable) back-to-back in one operator process via a supported lifecycle surface whose installed signature you inspected; never edit Kanban storage directly.
- If the race is already lost (implementer running), stop reissuing transitions or `reclaim` (the dispatcher respawns). Use `request-review <id> --reviewer <profile> --force`, confirm the reviewer claim, give the superseded worker one bounded grace window to exit, and re-verify worktree cleanliness and exact head.
- Permit review only when no prior-role process references the workspace, and the reverse after `request-changes`; run terminality and process exit are separate facts.
- After `hermes kanban request-changes <id> <reason>`, verify the card returns to the recorded implementer and a fresh run is claimed; comments are not transitions.
- If the card sits `ready` while ticks log `respawn_guarded: active_pr` after a valid changes request, treat it as a lifecycle defect: keep the same card/worktree/branch/PR and provenance, note the review run, rejected SHA, and corrections, and do one bounded owner-safe recovery via the installed supported rework path. Never delete PR history, open a new card/PR, loop unblocks, or implement in chat.
- `request-changes` (review or manual-inspection rejection) may bypass only duplicate-work guards (`active_pr`, recent success), never auth, rate-limit, dependency, or circuit-breaker guards.
- Never reopen a merged card into `ready` under the implementer, because the active-PR guard will churn; route it to post-merge review.

## Merge conflicts on an existing PR

- Treat a conflict as rework on the same card, branch, worktree, and PR, not a new workstream.
- Refresh GitHub `headRefOid`, `mergeable`, `mergeStateStatus`, the target, card/run state, and the worktree first, because a clean local checkout proves nothing about remote mergeability.
- Probe with `git merge-tree --write-tree <head> <target>`, list every conflicted and overlapping path, and state the semantic invariant to preserve.
- Never blanket-select `ours`/`theirs`, force-push, recreate the PR, or reset unrelated dirty/generated files.
- Record the diagnosis and route the original implementer with conflicts plus any open review findings combined, so the new head is reviewable, not just mergeable.
- If circuit-breaker blocked, get explicit authority for one `unblock`, then verify the `unblocked` event and a fresh `claimed`/`running` run on the same worktree.
- Call conflicts resolved only after the push reads back non-conflicting; then redo exact-head gates, QA, and review, keeping the human merge gate intact.

## Merge policy and auto-merge readback

- Project policy defines risk classes and auto-merge eligibility; fail closed on ambiguity.
- Explain a non-merge by the gate chain in order: exact-head full required CI, whether the merging reviewer ran, merge conflicts (`DIRTY`/`CONFLICTING` needs resolution and re-test; `BEHIND` alone is a gate only when project policy or branch protection requires up-to-date branches), then risk-class policy.
- Immediately before merging, re-read PR head, required checks, QA/manual evidence, review outcome, and whole-PR scope; afterwards read back PR state, merged-delta scope, and target reachability.
- Derive scope from GitHub's whole-PR files/commits, not only `approved-base..candidate`, because a branch on commits absent from the target merges unrelated ancestry. Any merged out-of-scope path is a blocking governance finding.
- For squash merges, a failed `is-ancestor <head> <squash>` is expected; verify the PR's recorded head, inspect `squash^..squash` paths/content, and confirm target reachability.
- Unexpected paths before merge: request changes. After merge: do not approve retroactively; ask the user to ratify the scope or open a narrow corrective/revert follow-up.
- A merge is not manual inspection, review approval, or scope ratification.

## Human merge gate

- Flow: review approved → blocked "approved; awaiting user merge of <PR> at <SHA>" → user merges and unblocks → reviewer verifies the merge came from the reviewed SHA and is reachable from the target → complete.
- Never complete a PR merged from an unreviewed replacement head; route it to review.
- Park a card waiting on the user's merge in `scheduled` with `hermes kanban schedule <id> <exact human predicate>`, not with repeated `needs_input` blocks, which trip `block_loop_detected` into `triage`. Parking from `triage` and the redundant children that auto-decomposition adds there are covered in `recovery.md` ("Human-choice and wall-clock waits", "Circuit breaker and late auto-decomposition"). Never `specify` a triage card just to park it, because it rewrites the title and body.
- Surface one concise decision; do not re-promote a reviewer to restate the same blocker. Merge-timing consent does not approve later replacement visuals.
- Release a `scheduled` card with `hermes kanban unblock <id> --reason "MERGE EVIDENCE: ..."` (`complete` rejects `scheduled`). Resolve shorthand like "1. merged" to the referenced card and run the readback directly.
- Repair a completion gate that fails repo-wide before asking the user to merge, because every completion will re-hit it.
- When the project policy drops the human merge gate, re-route each card parked as `approved; awaiting user merge` to review; do not merge it on the old approval. Steps:
  1. Confirm the PR head still equals the parked SHA.
  2. Restart the card's services at that head (the parked server may be stale), and check they respond over the reachable host.
  3. Write a reason file stating the new policy, that the candidate head is unchanged, the service URLs, any new review gates, the merge and closeout instructions, and that stale gates (for example exact-main CI) no longer apply.
  4. Release the card with `hermes kanban --board <b> unblock <id> --reason "$(cat <file>)"` and verify the reviewer claim.
  Earlier human inspection stays in the history but is no longer a gate.

## Completion contract and closeout

- Done for PR-backed work means merged and verified; for `local-only`/`manual-only` work, merge and reachability are `not_applicable`.
- Only the independent reviewer certifies delivery. The reviewer normally writes terminal completion; an operator may repair its ledger receipt only when project policy explicitly permits it and a durable reviewer-owned verdict already approves the exact merged head.
- Under a repository/PR contract, prepare the canonical URL before the first attempt: `--metadata '{"published_pr": "https://github.com/<owner>/<repo>/pull/<n>"}'`. A merge does not populate it; a bare repo name is rejected as `missing`.
- On rejection make at most one changed, preflighted retry; never repeat an identical call, because loops burn the budget and end in protocol-violation crashes. On a second failure, preserve the receipt and escalate.
- Read back `pr_acceptance`: require `ok: True`, the reviewed `head_sha`, and every required check classified success.
- Preflight acceptance with the assignee profile's GitHub auth, because the operator's ambient `gh` login can hide an assignee failure.
- Match required contexts and app IDs to checks published on the reviewed head. `gh pr checks --required` exiting nonzero with none configured means "no policy", not failed CI; renamed jobs or matrix shards do not satisfy a required context.
- Diagnose a repeated acceptance detail once at board level (`infra` is a catch-all; reproduce each phase). Never fake a status or silently edit a contract; a historical override needs recorded user authority and applies to one task.
- If a completion failed only on missing publication metadata, inspect what the store actually received before retrying. If the field never arrived, the defect is in how the call is built, and repeating the same call cannot repair it.
- For an authorized ledger-only repair, first read the reviewer-owned verdict, exact reviewed head, whole-PR scope, merged PR and target reachability. For a merge commit, also verify that its second parent equals the reviewed head. Replay the installed acceptance check as the reviewer profile and require `ok: True` at that head.
- Re-read ownership and exclude active claims, workers and worktree processes before closeout. If the supported CLI accepts the current blocked state, complete directly with `hermes kanban --board <board> complete <id> --summary <verified_delivery> --metadata '{"published_pr":"https://github.com/<owner>/<repo>/pull/<n>"}'`, without `--force`; do not unblock merely to launch another reviewer. Otherwise use the supported closeout route. Read back `done` and the successful exact-head `pr_acceptance` event. Repair only the receipt, never issue a new verdict or replay implementation.
- Closing an already-delivered `triage` card: restore `review` through `scripts/kanban_triage_route.py`, with a unique plan and `--dry-run` first. Preserve the recorded implementer, reviewer, stored completion contract, and canonical PR receipt. Do not use `specify` or `promote`: they reopen admission or implementation for delivered work.
- Keep the board dispatch lock continuously held from closeout preflight through supported review restoration, `complete --metadata`, and final `done`/acceptance readback. Separate commands each take and release the lock, so chaining them does not protect closeout; hold one lock across the whole sequence and reuse it rather than acquiring a second. Keep the route's preflight, provenance restoration, comments, and supported CLI intact, and never replace them with direct storage writes.
- If a route's readback fails because a worker already claimed the card, inspect the latest handoff, claim, run, and heartbeat: the transition may have succeeded and lost the readback race. Do not repeat the route, judge it from the exit code alone, or complete over the live worker; resume receipt repair only after ownership clears.
- `complete --force` closes a claim you know is superseded; it does not bypass acceptance or kill the worker. Confirm the run is terminal, then stop only that verified orphan process.
- `cannot complete <id> (unknown id or terminal state)` usually means already `done`; check live status first.
- When two guards refuse each other on a merged card (for example a respawn guard holds the implementer while a judge refuses completion), route it to review with explicit "review only; no implementation replay" metadata. Use `--force` only when the sole refusal is that review is incomplete.

## Post-merge exact-main CI is decoupled

- Gate delivery cards on the PR head and PR required checks only; exact-main CI is monitored and repaired by the dedicated CI workflow or SHA-scoped repair cards.
- A main-CI failure never blocks, holds, or reopens a merged delivery card; do not ask the user to rerun main CI for closeout.
- Reconcile a merged card blocked only on main CI: `hermes kanban unblock <id> --reason "target-branch CI decoupled to dedicated CI workflow"`, verify PR required checks, whole-PR scope, and merge reachability, then complete with the canonical `published_pr` receipt.

## Delivery vs ledger state

- Report delivery and ledger separately: merged and reachable but missing review or receipt → "merged; Kanban closeout pending", naming each missing gate.
- Only a stale bookkeeping gate left after all substantive gates passed → "delivery is complete; Kanban closeout is blocked"; repair the gate.
- Never call an unreviewed merge approved or Done, and never reopen implementation to normalize the ledger.
- Keep evidence commit-scoped (SHA, command, outcome, link, actor, time); append and mark stale, never delete. Quarantine only credential-shaped attachments via the supported API.
