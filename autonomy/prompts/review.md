Operate the initiatives review step for project `{project}` on board `{board}`. Profile `{profile}`; workdir `{workdir}`. Load `initiatives:initiatives`, `initiatives:kanban-human-gates`, and the project policy skill `{policy_skill}` when set.

Keep the Review queue equal to the decisions only the user can make. The separate recovery job does board recovery; this job does none and changes no card except through supported `daily_review.py` actions. The wake gate already wrote a fresh human gate list. If Script Output says the list failed, the file is gone: tick must warn, leave Kanban items unchanged, and resolve nothing. Continue the review and report this failure in one line.

Read `{review_tick}` and follow it exactly. Its placeholders:
- scripts: `{scripts_dir}`
- work files: `{work_dir}`
- gate list: `{gate_list_path}`
- topic: `{review_target}`

Budget: 8 minutes normally, 15 minutes when the daily digest is due.

Rules:
- Every human decision reaches the user through `daily_review.py notify` and the daily digest only. Never call `send_message` or send messages through other tools.
- Handle every stale item listed by tick through `daily_review.py sweep`: keep, refresh, resolve, or apply a recommended low/medium-risk option when the existing authority and supported action permit it. Refreshed items reach the user only in the digest.
- Exact-head human merge approval remains human-only. Never apply, settle, or resolve it autonomously; return any such auto item to the user. Never infer an owner or manufacture authorization.
- Use `terminal` for commands. Do not call `execute_code`, pipe output into an interpreter, or start processes from inline Python. Redirect each CLI output to a work file, then read the file.
- Re-read current state before mutation and read back every action. Never claim a queued card started without a fresh claim/run. Preserve exact-SHA evidence and the existing graph.

Reply exactly `[SILENT]` on success; otherwise one short plain line saying what failed (local delivery; the next run retries).
