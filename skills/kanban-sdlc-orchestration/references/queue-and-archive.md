# Queueing, Archiving, Cleanup, and Automated Intake

Read this when you queue work while dispatch is off, archive a card, clean up a worktree, or wire an inbox to create cards.

## Queueing while dispatch is locked

- Call a card in `triage`/`todo`/`ready` **queued**. It is started only when the intended worker has claimed it and it shows `running` with run evidence.
- Never enable dispatch as a side effect of creating a future task. Enabling it is a separate decision.
- Create with `hermes kanban create`, setting `--idempotency-key`, `--assignee`, `--project`, `--workspace`, an integer `--priority` per project policy, `--max-retries`, and a body (`--body-file` for multi-line) that states scope, acceptance, verification, and gates. Retries then return the same card.
  - Route rough work through `--triage`. Specified work takes the direct-ready path (see `intake.md`).
  - Keys match only non-archived cards, so reusing one after archival creates a new card.
- Add a lock comment with `hermes kanban comment`: the lock, the unmet prerequisite, and the exact release condition (branch/SHA if relevant). No secrets. Comments are history, not transitions.
- Read everything back with `hermes kanban show`, `hermes config get kanban.dispatch_in_gateway`, and `hermes kanban diagnostics`. Confirm queued status, no claim or run, correct assignee/project/workspace/branch, dispatch still off, and no stray worker.
- Before release, confirm:
  - the base branch is synced and approved, and setup/smoke gates are green;
  - the worktree path is viable and credentials are available;
  - retry and concurrency limits are intentional;
  - the body still matches the current request, and planning/inspection gates are present.
- Re-read the card after release and report it as queued until claim/run evidence exists.

## Archiving cards

- Archiving retires the ledger record. It does not authorize discarding unpublished WIP, so decide cleanup separately.
- For a superseded SHA-scoped card, re-check the target head, its exact-head CI, and that the branch has no unique commits and no open PR.
- Check how the installed `archive` handles workspaces first. Some versions remove a clean, remote-reachable worktree and may delete its auto-generated branch. If the worktree must survive and the installed guards cannot be shown to preserve it, keep the card blocked.
- Snapshot first: card, links, comments, events, runs, attachments, path, branch, exact HEAD, cleanliness, reachability, claim/run, process tree and cwds, servers, PID/port files, and inspection owner. Without it, a later loss cannot be attributed.
- If a worktree vanishes between snapshots, stop and record an **unattributed external disappearance**. Do not recreate it or treat its absence as proof of safety.
- Resolve the disappearance only if all hold:
  - the earlier snapshot proved it was clean at an exact HEAD;
  - that commit is still reachable from an authoritative remote ref;
  - no claim, run, process, server, or owner remains.

  Otherwise stay blocked (see `recovery.md`).
- Re-run the guards immediately before archiving, then archive the same card in place. Read back status and history/link/attachment counts.
- Record the preserved worktree, branch, HEAD, and dirty paths in a closeout comment, and report cleanup as deferred. Label any archive-driven removal as archive-coupled.
- Never delete a branch or worktree as an archive side effect, and never assume archiving removed branches, processes, or artifacts.
- Use `archive --rm` (permanent purge) only on explicit request, because it destroys the record.

## Worktree cleanup

- Clean up in a separate, targeted step: dry-run, then apply with the same canonical path and full expected HEAD, using the guarded wrapper named in the project policy reference.
- Re-check cleanliness, reachability, and ownership at apply time, so an archive decision never becomes deletion authority.
- Use normal worktree removal only: no force, no `rm -rf`, no branch deletion, no pruning other worktrees. Those destroy unrelated work.
- Never infer ownership from stale PID/port files, profile, or cwd alone. Find the live process before stopping it.
- Afterward, verify that the path is gone and unregistered, the branch still exists, and the card's history is intact.

## Automated document intake

- Use a script-only (`no_agent`) cron for mechanical intake because it is deterministic and cheap. Use an agent cron only when interpretation is required, with a self-contained prompt, since cron sessions cannot ask questions.
- Key each document by its inbox-relative path, not its content, so edits do not duplicate cards.
  - Keep a versioned state file mapping path to task id and content hash (audit only).
  - Ignore edits to processed paths.
  - Keep processed entries when files are removed.
- Process each unseen document as one transaction, in this order:
  1. Title it from the first H1, or a filename fallback.
  2. Create one triage card with explicit assignee, project, workspace, priority, limits, author, and the path key.
  3. Put the absolute source path in the body and attach the document.
  4. Add a notify+wake subscription when gates must reach a messaging destination, with destination ids kept in config.
  5. Checkpoint the path and task id atomically, only after steps 2–4 succeed.
- Make retries safe: the key recovers the same card, and existing attachments are checked before re-attaching.
- On failure, print the path and an actionable error, exit non-zero, and leave the path unprocessed for the next tick.
- Stay quiet: empty stdout with exit 0 means nothing changed, and `no_agent` sends stdout verbatim. Print one line per created card.
- Register the script by filename relative to the Hermes scripts directory. Read the job back (schedule, enabled state, target, next run) and confirm the scheduler is running.
- Test on an empty inbox (placeholders excluded): exit 0, empty stdout, no new card. Never add fake documents to a live board without authorization.
- On the first real document, verify the card, attachment, subscription, and checkpoint, and that the next tick adds no duplicate. Report the observed status: an active dispatcher may claim or decompose the card immediately.
