# Proof and measurement handoffs

Use this checklist in new cycle plans and delivery closeouts. Keep existing approved criteria unchanged;
propose missing commitments in the existing plan package, never adopt them as an agent finding.

## Plan proof with delivery

For each success criterion, specify:
- The exact release surface and how its SHA/version is verified. Merged is not released.
- The collector command/query, fixed cases or cohort, oracle, denominator and evidence artifact.
- Where the collector and synthetic fixture ship (existing command, or named delivery card/PR).
- The gate that proves the collector works before enablement. Do not promise an unwritten collector is running.
- The approved window, timezone and inclusive-start/exclusive-end timestamps, target and any independent
  evidence minimum. Do not invent dates or minima. Distinguish a usage target from an evidence requirement.
- Read/write/privacy boundaries, collection budget, stop conditions and expected failure receipt.

For a manual or existing collector, name its actual procedure instead of inventing a new script requirement.
A one-off approved spot check does not need a fabricated observation window.

## Hand off an outcome, not just a merge

Every closeout names four things:

| Handoff | Required content |
|---|---|
| Owner | Existing release operator and `outcome_review.owner`; actual Kanban card where one exists |
| Trigger | Verified release, named event or exact approved window boundary; timezone |
| Receipt | Release proof, metric/guardrail evidence and current-epoch report + proposal paths |
| Failure | Same owning card, exact failed prerequisite, evidence limitation, next owner and bounded remedy |

The existing Review tick dispatches assessment work subject to its cap and fresh Kanban reads. A configured
owner or collector is not proof of an active worker. Name a queued/blocked card accurately; do not call it
running merely because it is unfinished. If a trigger cannot be represented by the current scheduler, say so
and use the approved owner's existing card; do not claim new automation or add a cron/watch without authority.

Keep assessment cards in `assessment_cards`, never `kanban_cards`. A completed delivery card is not proof
that collection or outcome review happened. No silent restart, enlarged sample, or replacement-card chain.

## Classify evidence without changing the contract

- `met`: complete, comparable evidence satisfies every approved criterion and guardrail.
- `missed`: complete, comparable mature evidence falls below a target or breaches a guardrail. Low usage
  can be a measured shortfall, including zero observed calls when complete telemetry proves that value.
- `insufficient_data`: incomplete observations/window, or an explicitly approved minimum-sample rule prevents
  a judgement. A complete window does not waive an approved evidence minimum.
- `measurement_failed`: the collector failed; record the command, failure and bounded remedy.

If the contract conflates a target and an evidence minimum, quote it and propose clarification. Do not
reinterpret existing approval. Findings remain separate from the user's verdict and disposition.

## One decision package per choice

If a planning gate already owns the same unresolved contract choice, attach the assessment and proposal to
that plan owner and use the existing `duplicate_of Dnn` watch for the supporting cycle item. Preserve both
histories. Do not merge independent exposure, privacy, production, merge, UI or submission consent boundaries.
Do not create another approval card/graph or answer on the user's behalf.
