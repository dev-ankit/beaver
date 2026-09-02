# claudeagent — Claude Code (`claude -p`) text-to-SQL method

A drop-in BEAVER method folder (mirrors `myagent/`) whose agent delegates SQL
generation to **Claude Code in headless print mode** (`claude -p`).

## Backend
`agent.py::run_agent` builds one prompt from the question + schema (+ hints for
the active `--setting`), pipes it to `claude -p --output-format text` over stdin
(BEAVER prompts are ~30 KB), and extracts the SQL (`<ans>` / ```sql wrappers are
stripped by `clean_sql`).

### Config (env vars, optional)
| var | default | meaning |
|-----|---------|---------|
| `CLAUDE_MODEL` | claude's default | value for `--model` |
| `CLAUDE_TIMEOUT` | `300` | per claude-call seconds |
| `CLAUDE_BIN` | `claude` | path to the claude binary |

### Execution-guided modes (optional, gold-blind)
Same capabilities as `myagent`, ported to the Claude backend. All DB access is
mediated read-only by the agent process; the model never sees gold rows.

- **self-fix** (`CLAUDE_SQL_FIX=1`) — run own SQL; on an execution error, feed
  back *only the DB error* and ask for a corrected query (up to `CLAUDE_FIX_ATTEMPTS`).
- **explore/verify** (`CLAUDE_SQL_EXPLORE=1`) — run read-only queries against the
  real tables, inspect the rows *its own* queries return, self-check, then finalize
  (`CLAUDE_EXPLORE_STEPS`, `CLAUDE_EXPLORE_ROWS`). Combine with `CLAUDE_SQL_FIX=1`
  for a final error-repair pass.

```bash
CLAUDE_SQL_EXPLORE=1 CLAUDE_SQL_FIX=1 ./run.sh --dataset dw --setting 2
```
Needs the dataset's MySQL DB loaded + `MYSQL_*` creds (env or nearest `.env`).

## Files
| file | role | edit? |
|------|------|-------|
| `agent.py`   | Claude-backed `run_agent` (the seam) | swap backend here |
| `prompt.py`  | per-question context + hints per `--setting` (shared) | rarely |
| `execute.py` | runs the agent in parallel, resume support (shared) | no |
| `unify.py`   | reshapes into `unified-output/claudeagent/<run>/{generated,gold}` | no |
| `run.sh`     | CLI wrapper: execute → unify | no |

## Run
```bash
cd eval/claudeagent
./run.sh --dataset dw --setting 1 --q_fn dev_one     # 1-question smoke test
./run.sh --dataset dw --setting 1                    # full dev_sampled (100)
# CLAUDE_MODEL=claude-sonnet-4-6 ./run.sh --dataset dw --setting 1
```

## Score (from `eval/`)
```bash
RUN=<run name printed by run.sh>
uv run python evaluate_ex_acc.py --dataset dw --input_dir unified-output/claudeagent/$RUN
```
`evaluate_ex_acc.py` needs the dataset's MySQL DB loaded + creds in `.env`.
