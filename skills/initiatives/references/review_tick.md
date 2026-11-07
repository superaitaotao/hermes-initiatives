# Review tick

This runs every 30 minutes in the initiatives review cron, 15 minutes after each Kanban recovery tick.
Its wake gate writes the human gate list first, so the list always reflects the latest recovery pass.
It keeps the review queue equal to the decisions that only the user can make. Everything else is settled by an agent,
watched, or answered from records the user already made. A new decision reaches the user in the configured
review topic within this tick. Once a day, from the configured digest time, the tick also sends the full digest.

Paths on this host:
- scripts: `<scripts>`
- work files: `<workdir>`
- gate list: `<gates>`
- topic: `<target>`

## The bar

An item stays in front of the user only if it is one of these, and no earlier user record already answers it:

- approval of a high-risk plan, new scope, a new initiative or cycle, or a stage change;
- success criteria, a verdict, a disposition, a rank, or an idea rejection;
- a release, the release UI gate, a public production cutover, a merge, or a review verdict;
- a policy, security, credential, secret, privacy, or data-processing consent, or a waiver of any of these;
- a product or priority choice between options that each change the approved outcome;
- a conflict between two of the user's own recorded decisions.

"Agents could do it, but nobody asked" is not on the list. "It is important" is not on the list.

## Auto items (the user handed them to you)

The user marks an item **auto** in the Review tab ("Let agents handle it") or in the topic (`auto D12` or
`auto D12: <instruction>`). The mark is the user's decision for that one item: you decide it, and the user's note is your
instruction. `tick-in.json` → `auto` lists them with `user_note`, `options_current`, and the current options. Handle
every auto item in the same tick, before triage. Read its source exactly as for triage, then pick one:

1. **apply**: one current option is right. Follow `user_note` first; otherwise take the recommended option unless
   the live source shows it is wrong. Options that need confirm (archive, reject), merges, and rule changes are
   refused; return those. If `options_current` is false, write new options for it first (`write`, same format as
   step 3 below; the item stays auto), then apply.
2. **resolve**: nothing is left to do (the card finished, the decision is already recorded). Name the evidence.
3. **return**: you cannot decide it safely: the note conflicts with a recorded decision, the step needs a secret or
   a credential, or no option fits. Say what you need from the user in `why`; the user gets fresh options at the next tick.

**Append a rule.** Each auto mark tells you the triage asked the user something it should not have. For every `apply`
and `resolve`, find out why the item was not handled automatically and append a delegated rule for items like it
(`"rule"` in the entry):

- `name`: a short snake_case name for the kind of item; it is stored as `learned_<name>`.
- `why_not_auto`: the gap, starting with one of `no_rule:` (no delegated rule covers this kind of item),
  `rule_refused:` (a rule exists but its predicate failed; name the predicate and the evidence), `on_bar:` (the
  triage put it on the bar; name the bar line and why it does not hold here), `missing_evidence:` (the evidence was
  readable but the triage did not read it; name the source), or `misjudged:` (the options or recommendation were
  wrong; say what). Read the item's history (`daily_review.py history D12`) and its source for this.
- `when`: one sentence a later tick checks against the live source: which items this covers and what must be true.
- `match`: what narrows the rule, from `initiative`, `cycle`, `owner_profile`, `title_contains`, and
  `context_contains` (a list of words, at least 4 characters each). Use the narrowest match that still covers the
  items like this one; it must fit this item.

The code adds the step's signature (source kind, block kind or cycle reasons, action, and argument shape), so the
rule covers only the same step on items its match fits. If the case is one-off (the user's note was specific to this
item, or a learned rule already covers it), write `"rule": {"skip": "one_off: <why>"}` or `{"skip": "covered_by
learned_x: <why it missed>"}` instead. An apply or resolve without a rule or skip is refused. The result names the
appended rule in `rule_learned`; from the next tick on it is a delegated rule like the ones below.

Write `<workdir>/auto.json` as `{"auto": [{"short_id": "D12", "do": "apply", "option": "A", "why": "...", "rule":
{"name": "...", "why_not_auto": "no_rule: ...", "when": "...", "match": {"initiative": "..."}}}]}` and
run `python3 <scripts>/daily_review.py auto --file <workdir>/auto.json`. `why` is 1–2 sentences: what you checked
and why this step is right. Each result says what happened; a refused entry wrote nothing, so return that item. An
applied option runs through the normal executor (source recheck, read-back), writes a `by: user` delegation row and
a `by: agent` row with rule `auto_delegated`, and the item shows under "Settled by agents", where the user can overrule
it. Overruling suspends `auto_delegated`; then `auto` refuses everything and the marks stay until the user restores it.

Do not watch, defer, or settle an auto item; `write` refuses a watch for it.

## Stale sweep (items idle for 3 days)

The user's delegation: items nobody has looked at for 3 days get re-checked
every tick, and the sweep may clean them up without the user. `tick-in.json` → `stale` lists up to 8 of them, oldest first
(open, watching, deferred, failed or waiting for a confirm, with no new options, answer, action or deferral for 3
days; a date deferral still in the future is not stale). Items already in `items` are left out, because their
triage covers them. Handle each in the same tick, after the auto items. Read its source as you would for triage:
the card and its newest comments, the PR and CI, and the plan or cycle file. Then pick one:

1. **keep**: it still needs the user exactly as it stands. Nothing changes; it is checked again in 3 days.
2. **refresh**: the facts moved on. The plan is outdated, main or the live hosts no longer match what the ask
   assumes, a named prerequisite finished, a parked condition is met, or the options no longer fit. It returns to
   `items` at the next `tick`; write new options for it in step 3, with `why` naming what changed. These options
   are not pushed: the daily digest shows them marked "refreshed". If an agent should do the next step instead of
   the user, write a watch for it instead (`agent_owned:` naming the card).
3. **resolve**: nothing is left to decide. The card is done or gone, or the decision is already recorded. Name
   the evidence. Settled under `stale_sweep`.
4. **apply**: the recommended option is still right, and the step is low or medium risk under the
   project policy risk rubric (the project's `policy_skill` setting, when set). Give `risk`. Only open items
   with current options qualify. The code
   refuses: any option other than the recommended one; irreversible options (`confirm`); `attend` (a step only the user
   does); parking options; merges; `rule_state`; rule-learning and urgent items; the deep-tier merge gate; more than
   5 applies a day; and anything else while the user has suspended `stale_sweep` by overruling one of its settlements.
   The risk class is yours to judge, and the code cannot check it: never apply a bar item that is high risk (a
   high-risk plan approval, a credential or secret step, a public cutover, a verdict or a disposition). Keep or
   refresh those.

Write `<workdir>/sweep.json` as `{"sweep": [{"short_id": "D72", "do": "resolve", "why": "..."}, {"short_id": "D224",
"do": "apply", "option": "A", "risk": "low", "why": "..."}]}` and run `python3 <scripts>/daily_review.py sweep
--file <workdir>/sweep.json`. `why` is 1–2 sentences: what you re-read and what it showed. A refused entry wrote
nothing; keep or refresh that item instead. An applied option runs through the normal executor and writes one
`by: agent` row with rule `stale_sweep`. Applied and resolved items show under "Settled by agents", where the user
can overrule them.

The Kanban recovery job checks the card side: a human-gate card blocked over 3 days whose premise no longer holds
goes to the planner for amendment (the project's policy skill, configured by `policy_skill` when set →
`references/recovery-autonomy.md`). When the plan changes, the card's item changes too, and this sweep refreshes it.

## Triage every printed item, in this order

Read the item's context and, when that is not enough, the source: `hermes kanban --board my-board show <id>`, the
cycle file, the idea file, or the plan the item names. Search the user's recorded decisions before you ask: the cycle's
and initiative's `decisions:` rows, and the other initiatives' files when the topic is shared (release, hosting,
measurement). Stop at the first step that applies.

1. **Settle by rule** (`settle`, below) when a delegated rule covers it. The script checks the rule and refuses
   anything outside it; a refusal means the item goes on to step 2.
2. **Watch** when nothing is needed from the user now. `write` refuses a watch whose reason does not start with one of
   these grounds; name the evidence after it:
   - `machine:` the source says `BLOCKED — MACHINE` or "You: nothing needed" (name the comment);
   - `agent_owned:` a named card, profile, or job is doing the next step (name it);
   - `already_decided:` a recorded user decision answers it (quote the file and the decision date), and a named
     agent acts on it. If no agent will act on it, keep the item and say so in `why`;
   - `duplicate_of Dnn:` another open item carries the same decision (name it);
   - `waiting_on:` a named card, cycle, or date must change first (name it).

   For `agent_owned:` and `waiting_on:`, name the owner card by its id (`t_xxxxxxxx`). The sync reopens the item
   as soon as every named card other than the item's own is done or archived, because the item's basis tracks only
   its own card. `write` refuses a watch whose named cards have all finished already. A reopened item arrives in
   `items` with its old `watch_reason`: re-check the source and give options, or a watch on what still has to change.
3. **Options** only when it meets the bar above. One question per decision; if two items ask the same thing,
   keep one and watch the other as `duplicate_of`.

## Outcome assessments

`tick` first backfills missing open-cycle contracts within initiatives that already carry an enabled
`outcome_policy`, preserving its approval session and owner. It never opts unrelated initiatives in or
rewrites existing contracts. It then supervises enabled outcome loops and queues at most three active
assessment work units per project.
`outcome_assessments` reports created, pending and errors; this is agent work, not a user approval request.
Do not run long measurements inside this triage tick or delegate them to an untracked cron-only watch.
The linked assessment card owns collection, gap analysis and a bounded proposal. Do not create a second card
or link it as delivery. A main-CI/runtime cycle is not held behind a production deployment.

Cycle items with `outcome_review_ready` carry a current-input assessment receipt plus two documents. Read both:
compare results to the exact approved target/cohort/window; distinguish `missed`, `insufficient_data` and
`measurement_failed`; offer a verdict/disposition and the prepared bounded next-plan proposal. Adoption of new
scope, goal, deadline or cycle remains the user's choice. Missing criteria/dates should arrive with a concrete
proposal, never an invented commitment. Use existing `set_cycle_fields` (including `outcome_review`) for approved
contract edits, and preserve all unrelated fields. Do not conclude success from delivery alone.
A fully observed mature window below its target is a measured shortfall (`missed`), including low uptake;
`insufficient_data` means the approved evidence requirements are not met. Preserve explicit minimum-sample
rules even when the window is complete. Do not invent a minimum from a usage target.

Before offering a contract question, inspect the existing planning gate for that cycle. If the plan and
contract are the same unresolved choice, keep the plan gate as the decision package, give its owner the
assessment/proposal links, and watch the supporting cycle item as `duplicate_of Dnn`. Do not auto-collapse
independent production, privacy, exposure, merge or submission consents. No duplicate approval graph.

At every delivery closeout, check the proof handoff in `proof-handoffs.md`: collector delivered and exercised,
release/measurement owner, exact trigger, expected receipt, failure owner. `outcome_review.owner` is the
configured owner, not proof that a worker is running; use the live assessment card/status to claim execution.

### Routine work before a human question

Hash arguments use SHA-256 of the complete source file's bytes, and SHA-256 of the cited parsed user row encoded
as UTF-8 canonical JSON: `json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)`.
`default=str` preserves YAML date scalars as their ISO text. Index only `by: user` rows, not the full decisions list.

The user delegated routine outcome triage. `tick` returns `routine_candidates` even for current
options written on an earlier day; these are hints, not authorization to execute their recommended option.
Read each candidate's live cycle decisions, assessment report, proposal, and existing owner cards. Apply a
code-checked rule below only when every predicate holds. A refusal is not permission to use `auto`, answer as
the user, change the recommendation to force a match, or hide a real decision with a watch.

- **Faithful normalization:** use `normalize_outcome_contract` only to restore empty structured measurement
  fields from the exact typed values in an actual prior user approval. Prose extraction, suggested thresholds,
  new guardrails, new dates and changed scope are not normalization. A same-day initiative approval has
  ambiguous ordering and conservatively needs reconciliation too. Never restore a field deliberately cleared
  by the user; establish record drift from the source history before invoking the rule. Partial authority must
  not dismiss the unapproved remainder of a question. A restoration changes the assessment epoch: the old receipt
  becomes stale, and the remainder returns after a fresh assessment, subject to normal ownership/cap checks.
- **Bounded follow-up planning:** use `plan_outcome_followup` for one read-only investigation and repair plan
  against the cited approved cycle decision. The generated card does not implement the recommended option or
  select a product alternative. The recommended option's signature must be exactly
  `cycle:outcome_review_ready:kanban_create:<plan lane>`, and **every** option in the item must stay off the bar:
  one verdict, disposition, stage, measurement or `record_decision` option, recommended or not, refuses the whole
  item, so never settle the routine-looking option beside a protected one. Search for an existing owner first and
  reuse it; never create a duplicate graph. Execution, contract amendments and protected actions keep their
  existing approval gates.
- **Already decided, stale or duplicate:** use the existing withdraw/settle rules or grounded watches. An old
  approval only answers its actual scope; a new candidate-specific release or merge gate remains human.
- **Passing spot checks:** when the approved criterion is a one-off check and the assessment actually checks it,
  offer one evidence-backed verdict/closure decision. Do not require a new metric, baseline, guardrail, date or
  time window merely because the migration's template calls an empty field a gap. A diagnostic receipt may say
  `insufficient_data` for those missing fields while the report establishes the approved check. Explain the
  distinction and retain limitations. Do not manufacture a `met` receipt or change the user's criterion.

Missing optional bookkeeping is not a failed outcome. New criteria, changed measurement semantics, new dates,
new scope, verdicts/dispositions, releases, merges, secrets, permissions, privacy and production consent stay
with the user. Keep a genuine small-sample or unavailable-surface limitation visible.

`outcome_assessment_failed` means the assessment finished without usable proof. Diagnose the named card and
recover it through Kanban before asking for any product decision. `outcome_source_unavailable` means the live
source could not be read; keep the limitation visible and do not present fabricated options. A watch must name
an actual unfinished owner card or an explicit dated predicate, not merely a job believed to be working.

Cycle items with reason `cards_done`: every linked card is finished, but the cycle did not move (no verified
release, no successor card). Read the cycle's decisions, the plans it names, and the done cards' last comments for the
next step and its owner. A plan's unchecked user action ("U1. … decision") or a promoted idea with no card is the user's
decision: give options (create the card, change the stage, record the user action). Watch only with `agent_owned:`
or `waiting_on:` naming the open card that does the next step.

Criteria items: answer each cycle with `set_cycle_fields` and `fields.success_criteria`. `write` refuses
`record_decision` here, because a decision row leaves `measurement.success_criteria` empty and the gate never closes.

Cycle items with reason `reevaluation_due`: the user answered an earlier evaluation with a verdict and `await_evidence`,
and the observation window has ended. Read the cycle's criteria, guardrails and the evidence gathered since that
answer, then ask again. Options use `set_cycle_fields`: a verdict (`fields.result` with `verdict`, `summary`,
`evidence`) with a disposition and `fields.stage: concluded`; or keep waiting with a new
`fields.measurement` that copies the current measurement and changes only `observation_window` (`starts` = today,
`length_days`), so the item returns when that window ends. Never offer `await_evidence` without a new window, here
or on an `evaluation_due` item: an unchanged window makes the cycle `reevaluation_due` again at the next sync.

Cycle items with reason `verdict_due`: the release is verified and the cycle is judged by a one-off check (criteria
set, no observation window), so no evaluation date will ever arrive. Read the cycle's criteria and its newest
`release_verified` or `metric` evidence, then ask for the verdict against those criteria. Options use
`set_cycle_fields` with `fields.result` as a mapping (`verdict`, `summary`, and `evidence` naming the report),
`fields.disposition` as the action string (for example `stop`; it sets `disposition.action`), and
`fields.stage: concluded`; offer one option that asks for more evidence instead. Do not offer a measurement window:
the user chose a spot-check.

Cycle items with reason `disposition_due`: the user recorded a verdict but no disposition (for example "win, confirm in
production first"), and the observation window has ended. Read the verdict, its decision row, and the observations
recorded since, then ask how the cycle ends. Options use `set_cycle_fields`: `fields.result` (keep the verdict; add
`summary` and `evidence` naming the report), `fields.disposition` (`stop`, `continue` or `iterate` with follow-ups)
and `fields.stage: concluded`; or keep waiting with `fields.disposition: await_evidence` plus a new
`fields.measurement` that changes only `observation_window`, so the item returns as `reevaluation_due`.

Measurement readiness: a `measurement_missing` item names the missing fields. Admission opens it as its own item
(`cycle:<project>:<cycle>:measure`) as soon as a cycle is `decided` or `shipping`, so ask before delivery builds against
an undefined plan; a verified release adds the reason to that cycle's card gate instead. Never fold the admission ask
into a card gate: settle rules (`plan_after_cards_done`, `defer_behind_focus_cycle`) match `reasons` exactly. A cycle
that also lacks `success_criteria` is held by the initiative's criteria gate first: do not open a second item for the
same gap. Ask the user for the metric,
source, baseline, and observation window together; use `set_cycle_fields` with the measurement mapping or field
aliases (`primary_metric`, `source`, `baseline`, `observation_window`). A window is `{starts: verified_release | YYYY-MM-DD,
length_days: N}` with a non-negative integer day count. A criteria-based one-off spot-check may leave the window null;
do not invent a nominal one-day window. Missing metrics/sources/baselines still need a user decision.

A move to `measuring` must include any missing fields in that same option, and have a recorded `release_verified`
observation. Read and verify the release evidence first; never invent a release row to make the option pass. Option
preview and the final locked write both run the cycle validator. A rejected write leaves both fields and decisions
untouched. Existing invalid cycles allow incremental repairs and unrelated audit rows, but stage transitions must
meet all destination requirements. `record_observation.py` always preserves true observations; `cycle_warnings` in its
response means the release exposed an incomplete contract and the collector will ask for the missing fields.

Kanban items come from the human gate list (written after the latest recovery pass), so recovery already judged them human. Still apply steps 2
and 3: watch one only when its newest statement contradicts the list (`machine:`), or when it repeats another
item (`duplicate_of`).

## Delegated rules (`settle`)

The user delegated these rules; learned rules are appended from the user's auto
marks (below the table). `settle` checks each rule in code, writes an audit row with
`by: agent` and the rule in the cycle file, and lists the item under "Settled by agents" in the Review tab.

| Rule | Covers | args |
|---|---|---|
| `repair_promotion` | An idea that fixes a defect in work the user already approved: it repairs one of the target cycle's user decisions, and the cycle is decided, shipping, or measuring. Not for new scope, a new cycle, an initiative-sized idea, or a rejection. | `{"into": "<cycle id>", "repairs_decision": <0-based index among that cycle's by: user decisions>}` |
| `plan_exploring_cycle` | An `exploring` cycle with no cards whose next step is a plan or read-only analysis. It creates one plan card for the plan lane. The cycle comes back to the user as `plan_ready` when the card is done. Max 3 a day. Not when the cycle's first step is a credential, cost, security, or production change, and not when a user deferral or note says to wait. | `{}` |
| `plan_after_cards_done` | A decided cycle whose cards are all done, with a current recommended `kanban_create` for the project plan lane. Creates and links one card, leaves the cycle decided, and settles without answering as the user. Max 3 rule actions per rolling 24 hours. | `{}` |
| `plan_outcome_followup` | A fresh outcome-review recommendation for the configured plan lane gets one generated read-only investigation/repair-plan card. The recommended option must be exactly `cycle:outcome_review_ready:kanban_create:<plan lane>` and every option must stay off the bar; one verdict, disposition, stage, measurement or `record_decision` option anywhere in the item refuses it. Never executes the option's proposed implementation or selects a product alternative. Max 3/day; prior rule plans need a later user decision before another. | `{"repairs_decision": <0-based user row>, "authority_sha256": "<canonical row hash>", "source_sha256": "<whole cycle hash>"}` |
| `normalize_outcome_contract` | Restore selected empty measurement fields from the latest cycle `by: user` row's exact typed `fields.measurement` values; no newer initiative decision, existing value replacement, prose extraction or new contract adoption. | `{"authority_decision": <0-based user row>, "authority_sha256": "<canonical row hash>", "source_sha256": "<whole cycle hash>", "fields": ["source"]}` |
| `retire_suggestion` | A suggestion the review made itself, now replaced by a newer item or decision. | `{"replaced_by": "Dnn"}` |
| `withdraw_answered` | A request an agent filed (`request:*`) or a suggestion the review made, which a `by: user` decision recorded before the request already answers. Not for urgent consent items, and not when the user's decision only covers part of the ask. | `{"in": "<cycle or initiative id>", "decision": <0-based index among that file's by: user decisions>}` |
| `defer_exploring_cycle` | Park an existing exploring-only cycle review for a routine prerequisite wait that the latest user row explicitly authorizes. The item remains deferred, not settled. | `{"authority_decision": <0-based user-row index>, "authority_sha256": "<canonical row SHA-256>", "source_sha256": "<complete target file SHA-256>"}` |
| `defer_behind_focus_cycle` | Park a decided cycle with no cards while the initiative's `focus_cycle` — the cycle the user is pushing — has not reached measuring. The item remains deferred, not settled, and returns by itself. | `{}` |

For `plan_after_cards_done`, the only collector reason must be `cards_done`, with verdict and disposition unset.
The item must be open, attributed to the project, without urgency, owners, blocks, or any earlier answer.
Options must be current. The recommended card must name the configured plan lane and board, with a non-empty
title of at most 200 characters and only board, assignee, title, and body arguments. Every cycle marker must
name this cycle. Any option with confirm or an action outside defer, acknowledge, record_decision,
set_cycle_fields, and kanban_create refuses. Fresh sources must reproduce the exact collector basis, show an
active initiative and recorded decided stage, and have fresh Kanban data. A user decision in the cycle or
initiative dated on or after the item opened refuses. The chain guard requires a user row after the newest
rule audit row; failed rule actions on any item with this key also require a cycle user row dated strictly
later than the newest failure day. Same-day rows cannot establish that order.
Report a `failed` result with its card id and do not retry it. The next sync closes the item; when the card
finishes, the chain guard sends the cycle's next step to the user.

For `defer_exploring_cycle`, the latest `by: user` row must contain exactly this typed instruction:

```json
{"review_deferral": {"schema_version": 1, "rule": "defer_exploring_cycle",
 "purpose": "routine_prerequisite_wait", "cycle": "<target cycle>",
 "prerequisite": {"kind": "cycle_stage", "cycle": "<other same-project cycle>", "equals": "measuring"}}}
```

Only exact equality to `shipping`, `measuring`, or `concluded` qualifies. The prerequisite must still be pending.
Do not infer a prerequisite from prose, a signature, or historical answers. Do not backfill user rows.
Hash the row with UTF-8 `json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
Hash the complete current target file separately. Boolean indexes and unknown keys or versions refuse.
The current recommended option must explicitly use `defer` and `args.kind == "state_change"`.
Its arguments contain only `kind` and the exact `prerequisite` above.
The current open item must have fresh options, reasons exactly `["exploring"]`, and no prior answers or urgency.
Its initiative must be active. Linked or derived cards, changed ownership, or a later initiative user decision refuse.
All source intent and options must establish routine waiting. Every category in The bar still refuses.
Unknown or ambiguous intent, self/cyclic/cross-project instructions, dates, and indefinite conditions refuse.
The existing root `.review-write.lock` must exist; this rule never creates it during refusal.
Success returns an operation identity and verified receipt, appends one agent audit, and records no user answer.
A refusal writes nothing. A failure after preparation reports `failed` and keeps a recoverable operation.
Retry only the matching operation. Do not overwrite newer user edits or route around a suspended rule.
Same-basis collection keeps the parked item. Changed basis returns it through collection.
For explicit same-basis reopening, the command resolves the project and completed operation and verifies the current typed prerequisite.
A reason alone is not proof. Reopening records the old version and requests fresh options.
Rule-owned overrule works while deferred or reopened, binds to the completed operation, and suspends the rule immediately.

For `defer_behind_focus_cycle` the wait is the initiative's own `focus_cycle`, a field only the user sets. It applies to a
decided cycle with no cards whose only collector reason is `no_cards`, with `verdict` and `disposition` unset, in an
active initiative whose focus cycle is a *different* cycle of the same initiative, still `exploring`, `decided`, or
`shipping`. A verified release already counts as measuring, so the rule refuses then. The recommended option must be
the one `defer` with `args.kind == "state_change"`, no other option may carry a human gate, and an earlier answer that
was not an empty defer refuses. Cards, `next_action`, or a user decision dated on or after the item opened refuse.
Success appends one agent audit row with `by: agent`, the rule, `focus_cycle`, `focus_stage`, and `why`; it records no
answer and does not settle. The item returns to `open` by itself in the next `sync` when the focus cycle reaches
measuring, is replaced by another focus cycle, or is cleared, and `tick` then asks for fresh options. The user's own deferrals
are never returned this way. One overrule suspends the rule and returns the item at once; when the user's own later
deferral already replaced the park, that wait stays and only the rule is suspended.

**Learned rules** (`learned_<name>`) were appended from auto marks (above). `daily_review.py rules` lists them with
their `when`, `match`, `why_not_auto`, and state. `tick-in.json` → `learned_rules` lists open items an active learned
rule covers now, with the rule and its `args` (`{"option": "B"}` or `{"resolve": true}`). For each, read the item's
live source and check the rule's `when`. If it holds, settle it in step 1 like any rule: `{"short_id": "D20", "rule":
"learned_x", "args": {"option": "B"}, "why": "<which part of when you checked and what the source shows>"}`. The code
checks again that the rule is active, covers this exact step (signature and match), and is under its daily cap (5
items); options that need confirm stay refused. The decision row is `by: agent` with the rule and `learned_from`,
which never counts as the user's decision. If `when` does not hold, leave the item to triage and say which part failed in
the tick output; a rule whose `when` fails twice on items it was meant for is too wide, so say that in the reply and
teach a narrower one at the next auto mark (the user suspends it).

A suspended rule refuses everything (`settle` says why). The user suspends a rule by overruling one of its
settlements; do not route around a suspension with `watch` or a near-identical option.

A promotion target must be an open cycle: `write` refuses options whose `idea_status` `into` names a
`concluded` or `abandoned` cycle. Pick the open cycle that carries the work (often the newest one).

`why` is one or two sentences: the approved fact that makes the rule apply, with its file and date.

Write `<workdir>/settle.json` as `{"settle": [{"short_id": "D44", "rule": "repair_promotion", "args": {...},
"why": "..."}]}` and run `python3 <scripts>/daily_review.py settle --file <workdir>/settle.json`. Each result says
`ok` or the refusal; a refused item continues at step 2.

A deferred item whose named condition is met (for example "until #341 merges", and #341 merged) does not come back
by itself when its source file is unchanged. `tick-in.json` → `deferred` lists each parked item and its reason.
When the condition is met and you verified it live, run `python3 <scripts>/daily_review.py reopen <Dnn> --reason
"<condition>; <evidence>"`, then triage it like a new item in the same tick: step 1 (settle) first. A reopened
exploring cycle whose next step is a plan goes to `plan_exploring_cycle`, not to options.

## Rules for options

1. 2 to 4 options, ids `A`–`D`, one `recommend`. Each option names one action from the action list and its args. The
   label says exactly what will happen, in plain words, with the real ids ("Approve the removal matrix and unblock
   t_1234abcd"). One option should usually be `defer` (keep it parked). Merges are not offered: say "Merge PR #N
   in GitHub" as a `defer` option label if a merge is the decision.
   The executor runs `action`, never the label. `defer` parks the item and posts nothing to the card, so an approval
   must carry the action that delivers it. `write` refuses a gate whose options all only park it, and a `defer`
   option whose label approves ("Approve…", "Accept…", "Yes…").
   - The card resumes now: `kanban_unblock`.
   - Another agent acts first (a planner amends the plan, the operator prepares a receipt): `kanban_comment` on the
     card that agent picks up, with the label naming that agent.
   - The user does a step personally (an attended session, a login, a physical action): `attend` with
     `args.you` = that step in one sentence and `args.next` = what agents do after it. It posts the user's answer and a
     `**You:**` handoff to the card; the card stays on the user's "Waiting on you" list until it moves.
2. Every entry with options carries `summary`: `{"what": "...", "why": "..."}`, shown first on the card, in the
   push and in the digest. `what` is what is blocked and what waits on it (card names, not only ids). `why` is
   the actual cause, not the status: what failed or is missing, and since when. One or two plain sentences each,
   no jargon or acronyms the user would have to look up; `write` refuses an entry without it.
   The question is one sentence. `why` (the entry field) is 2–4 short lines: what each choice costs, and why you
   recommend one. Name the earlier user decisions that you checked.
3. Never offer an option that asks the user to type a password, login, code, or other secret in chat.
4. Every printed item needs an entry: watch or options (settled items need none). `write` rejects a missing entry,
   an unknown short id, an unknown action, or a watch without grounds, and writes nothing; fix the errors and run
   it again.
5. Plain language. Short sentences. No filler. Never invent a decision; an empty queue is a good result.

## Waiting on you

The Review tab's "Waiting on you" section and the digest list every card whose newest handoff comment says
`**You:** <a step>` (not "nothing …") while no open decision covers it, whatever its item's status. Write that
line honestly: it is how a parked approval stays visible to the user. A card that waits on the user must name the step;
one that does not must say `**You:** nothing needed`.

## Every tick

1. `python3 <scripts>/daily_review.py tick --gates <gates> > <workdir>/tick-in.json`, then read the file. `items`
   holds the items to triage now (new, reopened, or changed). `auto` holds the items the user handed to you.
   `deferred` lists parked items and their reasons. `routine_candidates` lists existing outcome recommendations
   to recheck under the bounded rules, even when their options are current. Inspect those in this tick before
   notifying; do not skip them just because `items` is empty. If `items`, `auto` and `routine_candidates` are empty
   and no deferral condition is met, go to step 4. `stale` lists items idle for 3 days (see "Stale sweep"): handle
   them every tick, even when `items` is empty. `handoff_cards` lists cards the tick just created for answered requests whose owner is a
   profile with no job (`review_handoff`); they need nothing from you, but say so in one line in your reply. A
   handoff error in `errors` repeats every tick until fixed: report it.
2. For each `unsummarised` decision (shown before summaries existed), check its card and write
   `<workdir>/summaries.json` as `{"summaries": {"D7": {"what": "…", "why": "…"}}}`, then run
   `python3 <scripts>/daily_review.py summarize --file <workdir>/summaries.json`. It changes no options and
   pushes nothing again.
   Handle every `auto` item (see "Auto items"). Returned items appear in `items` when you run `tick` again.
   Settle what the rules cover (`settle`). Reopen deferrals whose condition is met (`reopen`), then run `tick`
   again so they are printed.
3. Write `<workdir>/tick-out.json` as `{"items": {"D7": {"question": "…", "why": "…", "recommend": "A",
   "options": [{"id": "A", "label": "…", "action": "kanban_unblock", "args": {"board": "my-board",
   "card": "t_…"}}, {"id": "B", "label": "Keep it parked", "action": "defer", "args": {}}]},
   "D8": {"watch": "agent_owned: t_… (planner) writes the plan"}}}` (no `review_id`). Leave settled
   items out. Run `python3 <scripts>/daily_review.py write --file <workdir>/tick-out.json` until it prints
   `"ok": true`. Then run `settle` again for newly written recommendations covered by a delegated rule,
   before `notify`. A refusal stays for the user; never widen the rule to force a settlement.
4. `python3 <scripts>/daily_review.py notify --target <target>`. It pushes each new decision once, and is silent
   outside the configured push window. Each pushed decision carries inline answer buttons (one per option, plus `Defer` and
   `Agents handle it`) posted straight through the Bot API, because `hermes send` carries no keyboard; a tap runs
   the same executor a typed reply runs and rewrites that message with the outcome. The tap handler replies with
   the callback's own bot (`context.bot`), not with a token resolved from the process environment: the gateway
   process that receives taps can run under a different `HERMES_HOME` than the process that sent the push, and a
   rewrite by the wrong bot lands on that other bot's message id space. Any button failure falls back to the
   plain-text push, so this step needs no change.

## Status summaries (every tick, after step 4)

Each active initiative shows a short status summary on its card. Facts come from code; you write only the prose.

a. `python3 <scripts>/status_summaries.py tick --project <project id> > <workdir>/summary-in.json`, then read it.
   `due` lists the initiatives whose facts changed (or that have no summary yet), each with `facts`, an
   `evidence_hash`, and the `previous` text. `unchanged` ones were only re-confirmed. If `due` is empty, stop here.
b. For each due initiative write at most 600 characters of plain prose, in this order: where it stands (the focus
   cycle's stage and what is moving), what changed since `previous`, what is blocked and by what, and whether a
   decision from the user is open (`review_items.open` > 0). Use only the facts. Say "unavailable" for missing data,
   never zero. State no verdict, no success, no release readiness, and no recommendation. If `kanban` is not
   `fresh`, say card data is unavailable. Do not list card or decision ids except to name a blocker.
c. Write `<workdir>/summary-out.json` as `{"summaries": {"in-…": {"text": "…", "evidence_hash": "<from the input>"}}}`
   and run `python3 <scripts>/status_summaries.py write --project <project id> --file <workdir>/summary-out.json`.
   It refuses the whole file on any error and writes nothing; fix it and run it again. Copy the hash unchanged:
   if the facts moved after you read them, the next tick sees the difference and rewrites the text.

The page marks a summary stale when no tick has confirmed it for 70 minutes. A failed step here must not stop the rest
of the tick.

## Once a day

5. `python3 <scripts>/daily_review.py daily --due`. Exit 1: stop here. Exit 0: the digest is due. Then run
   `python3 <scripts>/daily_review.py learn --propose`. It reads the user's answers and files one `learn:` item for
   each kind of item the user answered 3 times in a row with the recommended option and no note. Do not write options
   for `learn:` items; they come with their own. Do not settle or watch them.
6. `python3 <scripts>/daily_review.py daily --gates <gates> > <workdir>/review-in.json`, then read it. It has the
   `review_id`, the open items, and per-initiative facts and updates since the last review. If it says
   `"reuse": true`, today's review is already written and only its send failed: go to step 9.
7. Re-triage every open item in `review-in.json` against the bar, as if it were new: a later decision may answer
   it, it may repeat another item, or a rule may now cover it. Settle or watch those now. Items you keep need no
   new entry.
8. Write `<workdir>/review-out.json` as `{"review_id": "R…", "items": {}, "summaries": {"in-…": "…"},
   "updates": {"in-…": "…"}, "suggestions": []}`. `items` holds the watches from step 7 and any item with
   `needs_options: true`. Summaries: for each active initiative, 2–3 lines on progress toward the goal, the focus
   cycle's state, and the next gate. `updates`: one short paragraph per initiative that changed since the last
   review, from `projects[].updates`. The digest shows each initiative as its own section followed by its
   decisions, so keep each text about its own initiative and do not list decision ids. Suggestions (optional, at
   most 3): something that meets the bar and that no gate raised, with at least one evidence path or URL, key
   `suggest:<slug>`. Run `daily_review.py write --file <workdir>/review-out.json` until it prints `"ok": true`.
9. `python3 <scripts>/daily_review.py send --target <target>`. A failed send is retried by the next tick.

## Learning items

`learn:rule:*` items ask whether a new rule should be planned. `learn:overrule:*` items follow an overrule: the rule
is already suspended, and the user picks keep, restore, or undo. Leave both to the user. If one is printed in `items` without
options, write a watch `agent_owned: review_learn wrote its options` and say so in your reply.

## Failures

If a review step fails and you cannot fix it within this tick, say so in one line in your reply (the reply is kept in the run log) and stop the review step; the next
tick continues. Never send review messages yourself with `send_message`; `daily_review.py` is the only path.
