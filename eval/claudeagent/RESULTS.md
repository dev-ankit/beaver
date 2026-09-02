# claudeagent — Claude vs Codex on BEAVER `dw`: results

Evaluation of the `claude -p` agent (`agent.py`), and a head-to-head against the
Codex agent (`../myagent`). **Execution accuracy** on the 100-question
`dev_sampled` set, scored against the live MySQL `dw` database. Same pipeline,
prompts, settings, and gold-blind enhancements as `myagent` — only the backend
differs. See `../myagent/RESULTS.md` for the per-lever Codex study.

## Head-to-head — best config (setting 2 + explore/verify + fix, effort high)

| Backend | exec acc | correct | SQL errors | empty | mismatch |
|---------|:--------:|:-------:|:----------:|:-----:|:--------:|
| Codex (`gpt-5.5`) — `myagent` | 34% | 34 | 0 | 2 | 64 |
| Claude (`opus`) — `claudeagent` | 32% | 32 | 0 | 2 | 66 |

The 2-point gap is within run-to-run noise; both drive SQL errors to 0 and share
a near-identical (mismatch-dominated) failure profile.

## Key finding: the backends are complementary

| | count |
|--|:-----:|
| both correct | 23 |
| only Codex | 11 |
| only Claude | 9 |
| **union (either)** | **43** |

Only 23 of their ~33 correct answers overlap — **~20 questions are solved by
exactly one model**. The union (**43%**) is far above either alone (+9 / +11).
Backend diversity is a larger lever than any single-backend knob measured
(hints +7, explore +3–4, effort/self-fix ~0), which points to **cross-model
ensembling** (generate with both, majority-vote on the execution result — still
gold-blind) as the path to ~40 %+.

## Backend
`agent.py` runs `claude -p --output-format text` (prompt over stdin; ~30 KB).
Supports `--model` (`CLAUDE_MODEL`, e.g. `opus`/`sonnet`) and `--effort`
(`CLAUDE_EFFORT`, e.g. `high`), plus the same gold-blind execution-guided modes
as `myagent`:
- **self-fix** (`CLAUDE_SQL_FIX=1`) — repair execution errors from DB error feedback.
- **explore/verify** (`CLAUDE_SQL_EXPLORE=1`) — run read-only queries against the
  real tables, inspect the rows its own queries return, self-check, finalize.

## Reproduce
```bash
cd eval/claudeagent
CLAUDE_MODEL=opus CLAUDE_EFFORT=high CLAUDE_SQL_EXPLORE=1 CLAUDE_SQL_FIX=1 \
  ./run.sh --dataset dw --setting 2

cd .. && uv run python evaluate_ex_acc.py --dataset dw \
  --input_dir unified-output/claudeagent/<run_name>
```
Requires the `dw` MySQL DB loaded + `MYSQL_*` creds in `.env`. Per-run SQL
outputs live under `eval/unified-output/` (gitignored — contains gold SQL from
the gated dataset).
