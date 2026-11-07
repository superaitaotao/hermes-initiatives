# Leaked fixture cards on a live board

A test or smoke probe that thinks it uses a disposable Hermes home can write real cards to the live board. These cards are not gates. They inflate status counts and confuse the human-gate report.

## Recognise

All of these together:

- Placeholder title (`no-code`, `c2`, `child`, `legacy`, `test`, …) and an empty body.
- Empty `created_by`, and a `blocked` event with reason `initial_status` (actor `user`), or other events from the code path under test (for example `closeout_audited`).
- `workspace_kind: dir` pointing into a temp dir (`<prefix>-XXXXXXXX/repo`), often under a profile `cache/scratch/`.
- Several cards created within a minute or two.

## Trace the source

1. Take the workspace dir prefix from `show`. Grep for it: `grep -rl "<prefix>" ~/.hermes --include=*.py --include=*.sh --include=*.md | grep -v /logs/`. This usually finds the probe script and any cron status report that already noticed the cards.
2. Read the probe header for its `sys.path` root (often a card worktree `.worktrees/<card>`). That card is the creator.
3. Find all fixtures, not only the ones that the user asked about. List with `list --archived --json` and filter on empty `created_by` plus the creation window. Siblings in `done`, `ready`, or `archived` exist too, and a pathless `scratch` child of a fixture is a fixture.

## Why the isolation fails

`get_default_hermes_root()` treats any `HERMES_HOME` under `~/.hermes` as profile mode and returns the real root. `kanban_home()` uses that root unless `HERMES_KANBAN_HOME` is set. A `tempfile.mkdtemp()` inside a worker resolves under the profile's `TMPDIR` (`profiles/<lane>/cache/scratch`), which is inside `~/.hermes`. Re-verify both functions in the installed source when the behaviour seems different.

## Retire

1. Post one `FYI` comment on the creator card in the Card comment format. Give the mechanism, the fix (temp dir outside `~/.hermes`, or `HERMES_KANBAN_HOME=<tmp>`, then read back `kb.kanban_db_path()` before the first `create_task`), the leaked ids, and a request to the reviewer to confirm that no test writes to the live board.
2. Do not archive while the creator card is `running`, because the probe may read its cards back. Use a deferred, self-removing, script-only cron job:
   - Each tick, exit silently while the creator card is `running`.
   - Then select by a strict filter (empty creator, creation window, probe titles, temp-dir or pathless-scratch workspace). Dry-run the filter once by hand and confirm it matches only fixtures before you schedule the job. The filter also catches cards from later probe runs.
   - `archive` each match, read back the statuses, print one summary line, and remove its own cron job.
   - Give it a finite `repeat`.
3. `archive` keeps `dir` workspaces (it removes only `scratch` and provably clean `worktree` ones). The probe's temp dirs stay on disk, so mention them. Do not use `archive --rm` unless the user explicitly asks for it.
4. In the gate report, subtract the fixtures from the status counts and name the real remaining cards.
