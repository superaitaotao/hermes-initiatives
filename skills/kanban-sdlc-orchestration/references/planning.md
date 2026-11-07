# Planning and Plan Gates

Read this when an initiative needs a plan before implementation, when a plan waits on human approval, or when a user supersedes an earlier durable gate.

## Plan-first graphs

- Plan first for anything with an attached design, an approval gate, unknown repository scope, a broad migration, deletions, or several subsystems. Create only the planner child, because coder cards made before the plan lock in guessed PR boundaries.
- For approval-gated work, keep `kanban.auto_decompose` off (verify with `hermes config get`). Don't rely on unrecognized config keys, because Hermes may ignore them.
- Treat auto-decomposed children as provisional until they're reconciled against the approved exact-file plan. A valid-looking graph can still be impossible to execute.
- Each reconciled child states its deliverable and PR boundary, expected paths, red/green and verification commands, runtime and iteration budget, checkpoint, dependencies, and gates. Record the plan version, measured file count, and test runtime so the release is auditable.
- Split independent deliverables even if the design groups them in one phase. For broad mechanical edits, name one generator and one deterministic validator, because per-file inspection burns the budget.
- Never put post-merge work (a measured run, a report, a data operation on the merged SHA) on a PR card. The reviewer completes a PR card at merge, so that work has no owner. Give it its own `local-only` card whose parents are the PR card and any operation it must wait for; the reviewer reads back its artifacts.
- A plan that defers scope ("later tranches are created after X lands") leaves that scope unowned: when X lands, the deferral becomes a decision point, so report the milestone together with the unfunded next step and get the scope enumerated in a plan before creating cards, because a card built on unenumerated scope locks in a guessed PR boundary. First confirm the whole chain is finished by reading the plan's card map and checking every work item is `done`; a completion notice names one card, not the campaign.
- Give the campaign's own closing work (baseline-vs-final measurement, hand-off report) an owner at plan time. It reads like a status update, so it is the item most often left with no card, no gate, and no one to run it.
- Chain PR cards that change the same function or file (parent links in order), because parallel PRs on one code path conflict at merge.
- When a child exhausts its iteration budget, persist the work, SHA or diff, passed gates, open items, and next command. Then split it inside the existing root graph or hand it to an owned continuation lane. Don't replay the same prompt or fabricate a new graph, because both lose provenance.
- A config write is staged, not active, until the running dispatcher is proven to use it. Keep production cards parked until then, and don't restart the gateway under live workers.

## Reviewable plan gates

- Write the plan as versioned Markdown in the durable task workspace, with the baseline SHA and risk class at the top. A plan in a worker scratch path isn't a gate.
- Include objective, scope, fixed decisions, expected files, TDD sequence, acceptance criteria, verification matrix, QA gate, risks, and merge policy. Put UI evidence in a sibling exact-SHA directory so evidence is tied to what was evaluated.
- Attach the plan and key evidence to the planning card, because an artifact path or summary gives the operator nothing to open. In chat gates, send the complete `.md` file as a native attachment every time, with a compact review map and the exact reply options. Confirm delivery before asking for review.
- The review map follows the plan's section order, then gives the file location and a way to open a rendered view.
- Block the planning card with `hermes kanban block <task> --kind needs_input "<question>"`. Don't complete the planner just because a draft exists, because completion would promote dependents.
- Release with `hermes kanban unblock <task> --reason "Approved plan <version/SHA>; ..."`. A plain comment records context but doesn't release the gate.
- After approval, complete the planner with a structured handoff (plan version, fixed decisions, file scope, tests, approval evidence). Only then may dependents promote.
- Planner completion isn't approval. Re-read the graph when it finishes. If children became runnable early, reclaim only verified-active workers and park the children without deleting their evidence.

## Multiple plan gates and scoped replies

- Re-read the live board and list only the blocked planning cards that need this user's decision now. Label each attachment with its card id and title. "Markdown files only" means the original uncompressed `.md` files: no archives, PDFs, or summaries.
- Tie each reply to the document or card it answers. Approving one plan never releases a neighboring plan.
- For a gate with several decisions, record a partial decision as a durable comment that names the choice, the user's authority, and what's still open. Leave the card blocked until every required decision is resolved.
- Treat "approve all" as covering only the set that the immediately preceding message listed. Record the exact plan version on each gated child before unblocking it, and leave unrelated gates closed.
- If a bare "approved" has no clear target, check the reply target and active gates. Ask if it's still unclear, because guessing could release the wrong gate.
- After unblocking, read back the card and its runs. `ready` means queued; report the card as started only after a new claim or run appears.

## Planner completed before approval

- Fail closed: keep the implementation card blocked and make no edits, because completion wasn't approval.
- Keep the completed run and its history exactly as they are.
- Surface the plan file, then record explicit approval on the implementation card or reopen planning. Unblock only after that decision is durable, and name the plan version and approval event in the handoff.

## Superseding a durable human gate

- Treat a waiver as a new user-authored decision, not proof that the old condition became true. A waiver changes policy; it doesn't create evidence.
- Re-read the card, runs, diagnostics, dispatcher config, and repo base state before any mutation, because acting on a stale view can damage a healthy card.
- Re-derive the card's recorded snapshot (file counts, artifacts, latest report, process liveness) from live state and post corrections. A worker released against stale figures may revert newer artifacts.
- Record the decision with `hermes kanban comment --author user <task> "..."`. Name the superseded instruction, the replacement, and exact accepted values, such as both local and remote SHAs when you proceed on an unsynced base. Say that sync was waived, not that it passed.
- Keep the old blocker, run, and comments, labeled as superseded. Never delete history.
- Choose the transition from the card's actual status, not from the word "resume":
  - For `blocked`, use `unblock --reason`.
  - For `triage`, run `specify` (triage → todo), then `promote` (todo → ready), because `promote` refuses a card that's still in triage.
  - Copy the body before `specify`, because it rewrites the body and the old text may not be recoverable. Read the new body back, re-post anything material that was lost, and reconcile its figures against the live snapshot.
  - If `promote` says the card is already `ready`, the dispatcher promoted it. Verify that, and don't retry.
- Changing dispatcher config doesn't unblock a card. Re-enable dispatch only if the user also authorized release.
- Before saying the card started, verify: it's claimed or running with a new run, the prior run is still visible, the worktree HEAD equals the accepted base SHA, diagnostics show no blocker, and the card hasn't re-blocked on stale instructions.
- Every other gate stays in force. Waiving base sync doesn't waive plan approval, tests, CI, QA, review, or merge policy. See `gates.md`.

## One-operation protected edits

- When a gate covers protected instruction or policy files, show the exact path allowlist and the semantic change. Never ask for blanket approval.
- Apply the approved change once, only to those paths. Read back each file, verify the invariant, and record content hashes plus an "approval consumed" note on the same card. Approval of an earlier patch doesn't cover later wording.
- Return the card to its implementer for fresh evidence and review. An operator patch satisfies no downstream gate.
- If approval is denied or times out, assume nothing was written and keep the card gated. Never retry through a different write path.
- Preflight the card's expected files for protected agent-instruction files and batch protected edits into one all-or-nothing patch, because consent grants one operation, not a path or session.
- A later chat "approve" authorizes scope but cannot resolve an expired prompt: record it as a user comment with exact paths and artifact digest, and do not claim the grant was consumed.
- Resume only through a verified live approval-capable surface, ask the user to select **Allow once**, then verify the approval, each protected-file readback, and the worker's terminal handoff. Allowlists and `--yolo` are not substitutes.
- If prompt delivery is unavailable or times out again, park the card in `scheduled`; re-read before any post-approval transition, since moving an active run to ready reclaims it.
