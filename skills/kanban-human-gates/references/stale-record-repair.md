# Repairing a stale durable record that blocks an otherwise-finished card

Two recurring shapes where the card's own bookkeeping — not the work — is the blocker. Both are machine-recoverable, so classify them under **mechanical closeout** or **machine predicate**, never as a human decision. Always verify the underlying delivery or authorization before repairing the record, and preserve the failed attempts as history.

## 1. Ledger closeout: merged and verified, but the completion receipt is missing

Signature: `pr_acceptance` events carrying `ok: false, classification: "missing"` with detail `Supply metadata.published_pr matching the persisted completion contract.`; the reviewer's runs crash, block, or `gave_up` after retrying the same call; the card sits `blocked` (or `triage` after repeats). The call arrives with an **empty metadata object** (`metadata={}`) rather than an omitted argument, so it is a tool-call shape defect and never a delivery defect — the merge is already done and verified while the gate refuses the receipt. Read the worker's own last comment before classifying: a worker that names the transport fault instead of claiming success is reporting accurately.

**Act while the run is still live.** The run is already past the point of discovering the correct shape, so comment the exact call on the card rather than waiting for it to exhaust itself, naming a real metadata object with the canonical `published_pr` URL and pointing at the `pr_acceptance` event's `detail` field for what the gate wanted. Treat a worker that blocks as `capability` instead of fabricating a receipt as correct behaviour: the block is a valid finding, and the operator repairs the ledger from verified merge evidence rather than resetting the lane.

Cause: the receipt gate compares `metadata.published_pr` against the card's persisted `completion_contract`. When that contract is a bare repo slug (`owner/repo`), the field must be supplied explicitly — a merge never populates it, and leaving it unset fails the gate before it reads any check, so the run dies on a delivery that is already complete.

Order of operations:

1. Read the card's `completion_contract` read-only (`file:<board>.db?mode=ro`; `PRAGMA table_info(tasks)` first, then `select completion_contract from tasks where id=…`).
2. **Prove the delivery before repairing the ledger.** Squash commit reachable from the target branch, PR head equal to the reviewed head, and merged delta equal to the reviewed delta. A repair that writes a receipt for an unverified delivery launders it.
3. Preflight the gate with the assignee's own auth — the operator's ambient login can mask an assignee failure:
   ```python
   import sys; sys.path.insert(0, "<hermes-agent root>")
   from hermes_cli.kanban_pr_acceptance import collect_acceptance
   r = collect_acceptance("<owner>/<repo>", "https://github.com/<owner>/<repo>/pull/<n>",
                          assignee="<reviewer profile>")
   print(r["ok"], r["classification"], r["head_sha"])
   ```
4. Complete on the same card with the canonical URL:
   ```bash
   hermes kanban --board <b> complete <card> \
     --result "Merged and verified: <squash> reachable from <target>; merged delta matches the reviewed delta. Operator closeout repair: the reviewer's completion calls arrived without metadata, so the receipt failed as missing." \
     --metadata '{"published_pr": "https://github.com/<owner>/<repo>/pull/<n>"}'
   ```

Traps:

- `complete` refuses a `triage` card (`unknown id or terminal state`). Run the field-free status transition first (see SKILL.md), then complete.
- `cannot complete <id> (unknown id or terminal state)` for a card you believe is open usually means it is already `done` — read live status before assuming a refusal.
- Do not reset the failure breaker to re-run the reviewer lane for this cause; the lane is healthy and the delivery is done. Repair the receipt.
- Do not repeat an identical failing completion call. One changed, preflighted retry, then repair centrally.
- This is a repo-wide defect, not a per-card one: it recurs on every card whose contract is a bare slug. Fixing the gate (or defaulting the field) is worth a card of its own; until then, expect to repair each delivery once.

## 2. Approved-plan digest mismatch

Signature: the worker blocks with a claim like "the plan file hashes to X but the card records Y", refusing to implement on integrity grounds.

Rule: distinguish **supersession** from **tampering** before escalating, and never accept the mismatch as self-evidently disqualifying.

1. Compare the plan file's mtime against the card's `created_at`. A plan edited *after* the card was created means the card snapshotted an older revision of a still-live document — the ordinary case, not corruption.
2. Corroborate with a sibling card created immediately after the plan edit; it will record the newer digest, which identifies the revision in force.
3. **Check the content, not the hash.** Search the live plan for the card's own decision identifier, its defect description, and its work-item entry. If all three are present, the work remains authorized: treat the current digest as authoritative, tell the worker to proceed and to note the bookkeeping discrepancy in its handoff, and do not block again on it.
4. Only a plan that no longer contains the card's decision is a genuine human gate — then surface the missing decision, not the hash.

A stale digest must not stall a card whose decision is still present in the live plan. Record the discrepancy as a card comment so the durable record can be corrected without stopping work.
