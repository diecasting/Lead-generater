# DISCOVERY_PHASE_A2_2_3_IMPLEMENTATION_REPORT.md

**Phase:** A2.2-3 Implementation — Safe GitHub Actions Dry-Run Controls
**Date:** 2026-08-14
**HEAD (pre-edit):** `f25a43c`
**Files changed:** `.github/workflows/daily_leads.yml` (+24, 0 deletions)
**Scope:** WORKFLOW ONLY — `main.py`, tests, requirements, config, search/filter/score logic untouched
**Commit/Push:** NOT performed (per instruction)

---

## 1. What Changed

Two additions to `.github/workflows/daily_leads.yml` only:

### 1a. `workflow_dispatch` inputs (L6–26)
```yaml
  workflow_dispatch:      # 允许手动触发
    inputs:
      dry_run:
        description: 'Dry-run mode: skip send_email() and save_sent_history()'
        type: choice
        options: ['true', 'false']
        default: 'false'
      freshness:
        description: 'Bing search freshness window (Day/Week/Month; Bing path only)'
        type: choice
        options: ['Day', 'Week', 'Month']
        default: 'Week'
      history_file:
        description: 'Dedup history file path; override to isolate experiments from sent_cache.json'
        type: string
        default: 'sent_cache.json'
```

### 1b. env mapping on the run step (L67–72)
```yaml
      - name: Run lead collector
        env:
          DISCOVERY_DRY_RUN: ${{ inputs.dry_run || 'false' }}
          DISCOVERY_SEARCH_FRESHNESS: ${{ inputs.freshness || 'Week' }}
          HISTORY_FILE: ${{ inputs.history_file || 'sent_cache.json' }}
        run: python main.py
```

The step-level `env:` **merges** with the job-level `env:` (BING_API_KEY / OPENAI_API_KEY / MAIL_*
remain available). It does **not** re-declare secrets.

---

## 2. Static Checks

### 2a. YAML syntax — PASS
Parsed with `yaml.safe_load` (PyYAML). The file is syntactically valid YAML.
> Note: the first parse raised `KeyError: 'on'` only because PyYAML 1.1 interprets the `on:`
> trigger key as a boolean — a test-harness quirk, not a file defect. GitHub's own parser handles
> `on:` correctly. With the key accessed as `True`, all structural assertions pass.

### 2b. Structural assertions — PASS
- `workflow_dispatch.inputs` = exactly `{dry_run, freshness, history_file}`
- defaults: `dry_run='false'`, `freshness='Week'`, `history_file='sent_cache.json'`
- run step `env` maps all three controls
- `schedule` block identical to before (`cron: '0 0 * * *'`)
- `actions/cache` key unchanged (`sent-history-cache-v1`)

### 2c. `git diff --check` — CLEAN (exit 0)
No trailing-whitespace / tab-in-indentation errors. The single emitted line —
`warning: ... LF will be replaced by CRLF ...` — is Git's pre-existing `core.autocrlf` conversion
notice (repo-wide setting), **not** a defect introduced by this edit. It does not fail the check.

### 2d. `git diff --stat`
```
 .github/workflows/daily_leads.yml | 24 ++++++++++++++++++++++++
 1 file changed, 24 insertions(+)
```

---

## 3. Production-Default Equivalence (REQUIRED)

The `||` guard is essential, not cosmetic. Without it, a **scheduled** run (which has no
`inputs` context) would inject **empty strings**, and `main.py` does
`os.getenv("HISTORY_FILE", "sent_cache.json")` — an empty string is a *set* value, so the default is
**NOT** applied → `open('')` crash; and an empty `freshness` would be forwarded to the Bing API.

With the guard, both triggers resolve to the production defaults:

| Trigger | `DISCOVERY_DRY_RUN` | `DISCOVERY_SEARCH_FRESHNESS` | `HISTORY_FILE` | Result |
|---|---|---|---|---|
| `schedule` (daily cron) | `false` | `Week` | `sent_cache.json` | **Identical to pre-edit production** |
| `workflow_dispatch` (defaults) | `false` | `Week` | `sent_cache.json` | **Identical to production** |
| `workflow_dispatch` (experiment) | `true` | `Week`/`Month` | `/tmp/exp_*.json` | Dry-run, isolated history, no email |

- **Schedule auto-run behavior:** fully unchanged (same env the code would have had with no inputs).
- **Default manual dispatch:** fully equivalent to production mode (real email + real `sent_cache` write).
- **Secrets:** untouched — still injected via job-level `env:` from `secrets.*`.
- **Cache:** `actions/cache` step and its key (`sent-history-cache-v1`) untouched.

---

## 4. How To Run The Safe Experiment (post-merge)

In the Actions tab → "Daily Leads Collection" → **Run workflow**, set:
- `dry_run` = `true`
- `freshness` = `Week` (then re-run with `Month`)
- `history_file` = `/tmp/exp_week.json` (and `/tmp/exp_month.json` for the Month run)

Each run: skips `send_email()` + `save_sent_history()` (code-guaranteed at `main.py:1508`), uses an
isolated dedup history, and uploads `leads_report.json` / `leads_report.html` as artifacts. Compare
the two runs' `discovery_metrics.json` to evaluate whether `freshness` is a real volume lever.

> Reminder (from A2.2-1 audit): `freshness` is consumed **only** inside `bing_search()`; it is a no-op
> on the DDG fallback. The Week-vs-Month comparison is only meaningful if `BING_API_KEY` is present on
> the runner.

---

## 5. Verdict

✅ Minimal, additive, backward-compatible patch implemented and statically verified.
✅ Production (schedule + default dispatch) behavior provably unchanged.
✅ No code/test/dependency changes; secrets and cache untouched.
❌ Not committed / not pushed (per instruction).

*STOP — implementation complete. No Discovery run executed, no commit/push performed.*
