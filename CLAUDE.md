# HoodScout: rules for Claude Code

This repo is PUBLIC. Everything in it, including data/, is world-readable.

## Roles
- Pete decides. Claude chat reviews and writes prompts. You are the only writer.
- The task list is the Notion "HoodScout Board" (HS-n IDs). Do not add task lists to repo files; BACKLOG.md is only a pointer. Report new tasks to Pete in your report.

## Git rules
- HOLD BEFORE COMMIT: show diff stat and test results, then wait for Pete's explicit "commit".
- Commit to main only. Never push other branches or tags: pushes to anything but refs/heads/main have been refused with HTTP 403 (branch delete and tag create, 2026-10-20), while pushes to main succeed. On a 403, report the exact error and stop. Do not look for a workaround.
- The collector pushes to main about every 15 minutes. Pull with rebase before working. Never commit files under data/ by hand, except Pete-approved appends to data/hypotheses.jsonl.
- A pull with --autostash unstages staged work. Re-stage and re-verify the content before committing.
- Never force-push or rewrite history. Do not touch the "Protect main" ruleset.
- Before ANY claim about history, branches or ancestry, run `git rev-parse --is-shallow-repository`. If true, run `git fetch --unshallow` first.
- Commit messages: short imperative subject; keep the attribution lines you were given.

## Secrets and data
- Secrets live only in GitHub Actions secrets (BLOCKSCOUT_API_KEY, OPENSEA_API_KEY). Never write one into a file, URL, log line, issue or report.
- Contract address, not ticker. Never invent or guess an address. Mark anything unverified as UNVERIFIED.
- No trading advice anywhere (code, docs, dashboard text, reports). Tier is a filter, not a signal.

## Evidence layer
- data/hypotheses.jsonl is append-only. Never edit or delete an existing line. A change is a new line that supersedes the old one, and only before anything has been evaluated against it.
- Never register or change a hypothesis without Pete's explicit approval. After any registration, run scripts/evaluate.py --blind and confirm it does not crash. Never run --unblind or --report before the 2026-11-09 read.
- No self-adapting logic. Thresholds change only through a new pre-registered hypothesis that Pete approves.
- Before a hypothesis's read date, never run label.py --summary or any script that prints a return. Blind process metrics only.
- Freeze rule: once an evaluator join exists for a hypothesis, never change its rule, exclusions, thresholds, population or deciding statistic. Any such change is a new hypothesis Pete must approve. Bug fixes must be shown verdict-neutral (AST-identical decision path or identical blind outputs).
- Read day (2026-11-09) follows docs/EVIDENCE_LAYER.md "Layer 3d" exactly: fresh full clone at the pinned commit, commit the audit first, then run `python scripts/evaluate.py --unblind --report` ONCE, then HOLD before committing the report.

## Workflows
- collect.yml and holders.yml are started by cron-job.org via workflow_dispatch; neither has a GitHub schedule. stocks.yml is dispatch-only too: the stocks collection runs inside collect.yml.
- Do not add a GitHub schedule back to collect.yml without Pete's approval (GitHub dropped most scheduled fires; the reasoning is in the file's own header).
- Two workflows do carry a GitHub schedule: nft.yml (twice daily, pre-dating the move off GitHub's scheduler) and watchdog.yml (hourly). watchdog.yml's is the deliberate one -- a slow backstop, not a precise alarm.
- Keep permissions minimal per workflow. Pass workflow inputs through env, never interpolate ${{ }} into run: blocks.

## Tests
- Run `python -m pytest -q` before every report. Report the pass count.
- test.yml is an alarm, not a gate. It only runs on changes to scripts/, tests/, requirements-test.txt and itself.

## Reports
- 15 lines or fewer unless Pete asks for detail.
- Lead with the outcome. Then: diff stat, test count, decisions needing Pete, anything unverified.
- State what you did NOT verify. Do not repeat the steps you took.
- Point to a file path for long output instead of pasting it.

## Pointers
- docs/EVIDENCE_LAYER.md (evidence design), docs/TECH_NOTES.md (technical notes), README.md (overview).
