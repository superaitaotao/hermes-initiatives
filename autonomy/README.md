# Autonomy layer

The plugin owns the Python gates and prompt templates here. `hermes initiatives setup` writes only small shell wrappers into the selected profile's `scripts/` directory: Hermes rejects cron script paths or symlinks that escape that directory. The wrappers execute the interpreter used at setup time against absolute paths in this plugin; do not move the plugin checkout afterwards without recreating the wrappers.

```text
hermes initiatives doctor --project demo
hermes initiatives setup --profile default --project demo --dry-run
hermes initiatives setup --profile default --project demo
hermes initiatives status --profile default
```

Setup supports `--no-recovery`, `--no-merge-gate`, `--schedule-review '15,45 * * * *'`, and `--schedule-recovery '*/30 * * * *'`. Run with Hermes' own Python environment (pydantic and PyYAML are required). Shell wrappers and the installation lock require POSIX/bash/flock.

## Settings

Settings live in the plugin's existing host-level `settings.json`; `HERMES_INITIATIVES_DATA_DIR` overrides that location. Run `doctor` before setup. A project needs `kanban_board`; configure `worker_profiles` to suppress worker progress chatter and `policy_skill` to provide its risk rubric. `github_repo` enables GitHub readback; leave it unset for local-only boards. `merge_reviewer` enables the optional deep-tier gate filer; leave it unset to disable filing. `cwd` (else `root`) sets the job workdir. The global `timezone` controls schedule pinning and displayed timestamps; empty means local time. `review_target`, push hours and digest time remain the existing review subsystem settings.

- Recovery reads `kanban_board`, `github_repo`, `worker_profiles`, and `timezone` (`Settings.zone()`). Explicit `--board`, `--repo`, and repeatable `--worker` override project settings. `--candidates <script>` is a generic repeatable inventory hook. Cloud Run readback is **off by default**; opt in with repeatable `--cloud-run-service NAME` and `--cloud-run-region REGION`. Setup does not enable cloud or candidate hooks automatically.
- Human-gate detection reads `kanban_board`, `timezone`, and `Settings.push_hours()`. Its state is under the cron profile's `HERMES_HOME`; `--gates-json` bypasses quiet hours because the review owns notification delivery. A failed list is removed, never reused.
- Review wake reads the active profile's jobs/state, the project board, all configured project roots for file/outcome/summary changes, and the shared review store. The delegated daily-review command reads `digest_time()`, review target and push window. New gates can wait for the configured recovery pass, bounded by `--hold-max-min`; with recovery disabled there is no hold.
- Merge filer reads each selected project's `kanban_board`, `merge_reviewer`, `root`, and the global `review_target`. Notification uses the review subsystem's configured push window. The exact SHA and reviewer gate comment must match. Unset reviewer means no-op. `--project` limits its scope; Delivery always uses the settings `review_target`, the only topic where answers are read.
- Setup reads `kanban_board`, `policy_skill`, `merge_reviewer`, `cwd`/`root`, `timezone`, and `review_target`. Runtime flags resolve repo/workers from settings rather than freezing them in wrappers. Recovery instructions route unresolved ownership to the configured `plan_assignee`, never a guessed lane.

## Installation contract

Job names are `initiatives:<project>:recovery`, `initiatives:<project>:review`, and optionally `initiatives:<project>:merge-gate`. Wrapper names are `initiatives-<project>-recovery.sh`, `initiatives-<project>-review.sh`, and `initiatives-<project>-merge-gate.sh` under `<profile HERMES_HOME>/scripts/`. Each gate resolves `--job name:<name>` against that profile's `cron/jobs.json`, avoiding a fabricated job ID.

For each missing job, setup holds a profile-local setup lock, writes its wrapper, runs `hermes -p P cron create --paused --script <wrapper> --deliver local --failure-deliver local ... <schedule> <prompt>`, reads back the assigned ID by unique name (and checks `Created job: <id>` when printed), runs `hermes -p P cron edit <id> --script <wrapper>`, then `hermes -p P cron resume <id>`. Review/recovery attach bundled skills plus `policy_skill`; the merge filer is `--no-agent` every two minutes. Failures before resume leave the job paused. Existing named jobs are reported unchanged, not resumed, edited, or duplicated; inspect/repair a partial install manually. `--dry-run` prints full wrapper content and commands with assigned-ID markers and writes nothing.

The final stdout line `{"wakeAgent": false}` suppresses review/recovery agent runs. First run wakes and seeds history; changes, prior failures, resume checkpoints and a three-hour full-inventory backstop wake agents. Gate failures fail open. Human-list mode is silent and writes a JSON list; the no-agent merge filer is silent when it files nothing (neither is an agent wake gate).

Review-work files are per-project (`review-work/<project>/gates.json`) so separate project jobs cannot overwrite one another's lists. The existing review collector remains host-wide; it preserves other boards' items when no current list is supplied, rather than interpreting them as resolved.

## Verification

Run `tests/test_autonomy_*.py` with Hermes' Python and source on PYTHONPATH. Tests isolate HERMES_HOME, HERMES_KANBAN_HOME and plugin data. The real-CLI installation test creates jobs only inside its disposable test profile. The bundled SDLC helper tests run separately under `skills/kanban-sdlc-orchestration/scripts/tests`. No production profile or existing cron job is needed.
