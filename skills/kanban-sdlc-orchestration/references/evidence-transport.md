# Evidence Transport Recovery

Read this when a worker produced evidence (media, manifests, reports, reviewer captures) but attachment, publication, or closeout failed or timed out. Recover the existing bytes; never rebuild or recapture the candidate to replace them. Script: `scripts/kanban_attach_inventory.py`.

## Rules

- **Identity is bytes, not names.** Match stored attachments to sources by size + SHA-256. Stored filenames are not stable (the store may rename duplicates), so a name lookup can miss an existing upload and a retry can duplicate it. Take a fresh upload's id from the attachment list before and after the call.
- **Inventory first.** Count expected items from the sources themselves (media, retained recordings, named proof files), because a manifest may not list everything.
- **Absence needs proof.** A card view that omits attachments does not prove there are none; query the attachment list.
- **History is append-only.** Keep incomplete or wrong copies and attach the full original under a distinct versioned name. A summary or receipt is not the artifact it describes. Never edit a manifest after the fact, because that changes its hash; record outcomes in an additive receipt.
- **Transport is not a gate result.** Finishing transport does not satisfy pending CI or review, and does not justify dispatching a replacement run.

## Procedure

1. Re-read status, claim, run, PID, candidate SHA, and PR head. Stop if another worker owns the card.
2. Build the inventory and verify every source's size and SHA-256 before uploading.
3. List existing attachments and reuse any whose stored bytes match.
4. Transport the rest through the narrowest supported path: the worker's own attach tool, or `artifacts=[...]` on its next terminal call. If worker transport is refused, use the operator CLI (`hermes kanban --board <b> attach <card> <file> --name <sha-labelled-name> --author <operator>`; check `attach --help` first). Never bypass a worker ownership guard.
5. Verify each stored attachment against its source, and the total against the inventory. Append each success to a scratch receipt so retries survive interruption.
6. Comment the frozen SHA, verified attachment ids (source and stored names), and each remaining predicate. Leave the card non-runnable until those predicates clear; then resume the same owner without rebuilds, recaptures, or unrelated edits.
7. Run `dispatch --dry-run` after the transition. If a respawn guard blocks the resume, follow `recovery.md` → "Respawn-guard holds".

## Reviewer captures

- When the reviewer's own captures are the only missing item, take paths, sizes, and digests from its block reason, verify and attach them through the operator CLI, then plain-`unblock`, which returns a review-origin block to `review`, not to the implementer.
- Reviewer captures live in that lane's scratch space and may be pruned when idle, so attach them promptly; never rebuild the candidate to replace them.
