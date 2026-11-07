# Verifying a CI timeout or resource gate before you relay it

Use this when a card stops on a CI job-limit, per-file cap, or "runner resource" rule, for example a plan stop rule that sends backstop and resource failures to the user. The worker's diagnosis is evidence to test, not a verdict. Do these steps read-only before you explain the gate or recommend an option.

## 1. Split the failing PR CI run into failure families

1. List the jobs at the exact PR head: `gh api "repos/<o>/<r>/actions/runs/<run>/jobs?per_page=100" --jq '.jobs[]|[.id,.name,.conclusion,.started_at,.completed_at]|@tsv'`.
2. For each failed job, run `gh run view -R <o>/<r> --job <id> --log | grep -E "FAILED|Summary:|exceeded|SIGKILL|✗ tests"`. Each failure falls into one of three families:
   - an assertion failure, which is ordinary rework and not a resource gate;
   - a file-cap kill (`300s exceeded; process tree SIGKILL'd`);
   - a lost runner.
3. If `--log` returns nothing for a failed job, read `gh api repos/<o>/<r>/check-runs/<job>/annotations`. The message "The hosted runner lost communication with the server" means the job died with steps still `in_progress`. Report it as a lost runner, not as a test failure or a timeout.
4. Re-read every job and diagnostic run the worker's comment called "in progress", "pending" or "unverified". Those claims go stale, and a job that has since ended as a failure, or a diagnostic run whose artifacts are now downloadable, changes the picture.
5. Find the test that was running when a file-cap kill landed. The kill block (`300s exceeded; process tree SIGKILL'd`) lists the file's tests that `PASSED`, then names one test with no result: that is the one that hung. When the other tests passed at normal speed and the same test fails in a separate diagnostic run, the kill comes from a hang in that one test, not from a slow machine. Treat it as its own failure family: an intermittent hang.
6. For a failure in a test file the PR never touched, check `git diff --name-only <baseline> <head>`. If the PR changes neither the file nor its subject, report it as not caused by the card and route it separately. Say whether `main` reproduces it, or that you did not check.

## 2. Check whether the diagnosis run caused its own symptom

Temporary diagnosis branches often add a pytest plugin, such as a `faulthandler.dump_traceback_later` timer or a `psutil` child-process and file scan every few seconds. On Windows runners, that overhead can push a file past its cap.

- Compare per-test durations in the diagnosis log (`DIAG CASE … call passed <s>`) with the same file's total in real PR CI at the same head. The runner prints `✓ <file> (<n>✓, <secs>)` and a slowest-files table.
- Suppose the file passes well under the cap in real CI (say 75 s against a 300 s cap) but is killed every time in the diagnosis run. Then the symptom most likely comes from the harness. Recommend a control run without the plugin before anyone plans a fix for it.
- Read the diagnosis workflow and scripts from the temporary branch head: `git fetch origin <sha>`, then `git show <sha>:<path>`. They are kept off the delivery branch on purpose.

## 2b. Read the diagnostic artifacts, not only the job log

The job log shows the assertion. The artifacts show why it fired.

1. `gh run download <run> -R <o>/<r>` into a scratch dir. Per-case folders usually hold the fixture's on-disk log, the script's captured return or stdout, and the runner reports.
2. Find the failing cases: `grep -c '"failed"' */*/*/reports.jsonl`.
3. Compare the log on disk with the captured output for the same case, both in a failing case and in a passing case. Suppose the captured output has the line the test waits for (for example `exit code: 0`) but the file on disk stops early, and the output shows write errors such as `Add-Content : Stream was not readable` that the script swallows with `try { … } catch {}`. Then log lines were silently dropped by a reader racing the writer. A test that waits for that line will wait out its full timeout.
   Then tally the suspected signal against the outcome across every case, so the claim rests on a correlation rather than on one failing example:
   ```bash
   for f in $(find . -name handoff-return.json); do d=$(dirname "$(dirname "$f")")
     st=$(grep -q '"failed"' "$d/reports.jsonl" && echo FAIL || echo pass)
     echo "$st $(grep -o 'Stream was not readable' "$f" | wc -l) $d"; done | sort
   ```
   Every failure having the signal and every pass having none, on both architectures, makes it the leading cause. Report it as a correlation until a deliberate reproduction confirms it.
4. Check who added the reader. `git log <baseline>..HEAD -- <test file>` names the commit that added it. When a user-approved trigger change made the test wait for a line in that log, the defect came in with that change. Say so neutrally: the approved decision is unchanged, but its implementation needs a different signal (for example, a sentinel file or a pipe the script writes). When the PR added the helper that repeatedly reads the file the script is still writing, the hang is a defect in the PR's own test code. Name the swallowed write as a second, product-side gap. Both are inferences until a reproduction shows the race, with a control run that does not have it.

## 3. Attribute new red tests to the runner image

A test the PR never touched may pass on the previous run and fail now. Compare the runner image in both job logs: `grep -E "Image: |Version: 20"` near the top of the log. When the image version changed between the green and red runs, an image regression is the likely cause. Route it to its own fix rather than charging it to the card. Confirm the untouched file with `git log <baseline>..HEAD -- <test file>`. Say that it is unverified until upstream or another branch shows the same failure on the new image.

## 4. Shape the recommendation

- Name which failure family the gate's offered option actually targets. Options are often worded around the worker's headline symptom, and that symptom can be the one most likely to be an artifact.
- If the evidence contradicts part of the brief, recommend the option with a narrowed brief:
  - run the control first;
  - split the image regression into its own fix;
  - re-run a lost runner once before calling it a resource failure;
  - for an intermittent fixture error, capture the real exception first;
  - for an intermittent hang localized to one test, fix the race. Do not add a rerun-on-kill exception: a rerun then passes by luck, which is a retry, and it merges the race-prone test. That contradicts earlier decisions to keep retries unchanged.
- Re-judge every offered option against the cause you found. Raising the cap does not help a test that waits out a fixed internal timeout, and changing the runner strategy does not help a race that will follow onto any runner of the same OS.
- Keep the user's earlier decisions and the plan's stop rule intact. Narrowing what the next plan targets is not a new decision about limits, retries or assertions.

## gh pitfalls met on fork repos

- `gh run list --workflow CI` can fail with "could not resolve to a unique workflow" when `ci.yml` and `ci.yaml` both exist. A fork whose default branch lacks the workflow can return 404 on `--workflow ci.yml`. Use `--branch <b>` without `--workflow`, or the run's jobs API.
- `hermes kanban runs <id> --json` may print a non-JSON table on some releases. Read run outcomes from the `Runs (n):` section of `show <id>` instead of piping into `json.load`.
