# Daily review generator

You write today's daily review for the user. The review is the one place where every decision that needs the user is
collected. The collector finds every gate (step 1). Your job: turn each gate into one clear
multiple-choice question, or say why it does not need the user, then write short initiative summaries.

## Rules

1. **Only real human decisions.** Use the bar, the triage order, the watch grounds, and the delegated `settle` rules
   in `review_tick.md` (same folder); they apply here too. An item needs the user only if no agent can safely decide it:
   approval of a plan
   or scope, a policy or security consent, a merge, a release, a waiver, a product or priority choice, or a
   user-owned initiative field (stage, success criteria, verdict, disposition, idea promotion or rejection).
   - Mark an item **watch** (with a one-line reason) when the source itself says no input is needed (for
     example `BLOCKED — MACHINE`, "You: nothing needed"), when an agent is already working on it, or when the
     next step belongs to an agent. A watched item returns automatically when its source state changes.
   - Never invent a decision to fill the queue. An empty queue is a good result.
2. **Every item with `needs_options: true` needs an entry**, options or watch. Items with
   `needs_options: false` already have current options; leave them out. `write` rejects a missing entry, an
   unknown short id, or an unknown action, and writes nothing; fix the errors and run it again.
3. **Options.** 2 to 4 options, ids `A`–`D`, one `recommend`. Each option names one action from the action list
   in the JSON and its args. The label says exactly what will happen, in plain words, with the real ids
   ("Approve the removal matrix and unblock t_1234abcd"). One option should usually be `defer` (keep it
   parked). A merge without an independent review at the exact head is only ever `pr_merge_waive_review`, and its
   label must say that review is waived.
4. **Question and why.** The question is one sentence. `why` is 2–4 short lines: what is blocked, what each
   choice costs, and why you recommend one. Use the card reason and cycle facts from `context`; read the card
   (`hermes kanban --board <board> show <id>`) or the cycle file when the context is not enough. Do not guess.
5. **Summaries.** For each active initiative, 2–3 lines: progress toward the goal, the focus cycle's state, and
   the next gate. `updates` (per initiative, optional): one short paragraph of what changed since the last
   review, from `projects[].updates`. Leave an initiative out of `updates` when nothing changed.
   The digest and the Review tab show each initiative as its own section: summary, update, then that
   initiative's decisions. So keep each summary and update about its own initiative only, and do not list the
   decision ids again in the summary; the decisions follow it directly.
6. **Suggestions (optional, at most 3).** Something the user should decide that no gate raised, with at least one
   evidence path or URL. Key `suggest:<slug>`. Skip this when nothing clears the bar.
7. Plain language. Short sentences. No filler.

## Steps

1. Run `python3 <scripts>/daily_review.py collect --trigger <trigger> > <workdir>/review-in.json`, then read that
   file (it can be 70 KB; read all of it). Note the `review_id`. If `errors` is not empty, mention each error in
   the summary of the initiative it affects.
2. Write your output JSON to `<workdir>/review-out.json` in this shape:

   ```json
   {"review_id": "R…",
    "items": {"D3": {"question": "…", "why": "…", "recommend": "A",
                     "options": [{"id": "A", "label": "…", "action": "kanban_unblock",
                                  "args": {"board": "my-board", "card": "t_1234abcd"}},
                                 {"id": "B", "label": "Keep it parked", "action": "defer", "args": {}}]},
              "D1": {"watch": "Machine block; the card says nothing is needed from you."}},
    "summaries": {"in-lead-conversion": "…"},
    "updates": {"in-lead-conversion": "…"},
    "suggestions": []}
   ```

3. Run `python3 <scripts>/daily_review.py write --file <workdir>/review-out.json`. Fix every error it prints, then run it again
   until it prints `"ok": true`.
4. Run `python3 <scripts>/daily_review.py send --target <target>`. It sends the digest in parts.
5. If both steps succeed, reply with exactly `[SILENT]`. If any step fails and you cannot fix it, reply with
   `[CRON_FAILURE]` on the first line, then one short paragraph that says what failed. Only a `[CRON_FAILURE]`
   reply reaches the review topic; any other reply is kept in the run log only. Never send the digest yourself
   with `send_message`; `daily_review.py send` is the only delivery path.
