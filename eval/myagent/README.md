# myagent — Codex-backed text-to-SQL method

A drop-in BEAVER method folder (mirrors `fewshot/`) whose agent delegates SQL
generation to the local **Codex CLI** via `codex exec` (headless, one-shot).

## Backend
`agent.py::run_agent` builds a single prompt from the question + schema (+ hints
for the active `--setting`), runs:

```
codex exec --sandbox read-only --skip-git-repo-check -C <tmp> \
           -c model_reasoning_effort=<effort> -o <last_msg> "<prompt>"
```

in a throwaway working directory (Codex never touches the repo), and reads the
final agent message back via `--output-last-message`. `<ans>…</ans>` / ```` ```sql ````
wrappers are stripped by `clean_sql`.

Uses your existing Codex login. **With a ChatGPT login, only Codex-supported
models work** (e.g. `gpt-5-codex`, `gpt-5.5`); arbitrary names like `gpt-5-mini`
are rejected — so BEAVER's `--model` is a label only and is *not* forwarded to
Codex. Pick the Codex model with `CODEX_MODEL`.

### Config (env vars, optional)
| var | default | meaning |
|-----|---------|---------|
| `CODEX_MODEL` | account default | Codex model (`codex -m`) |
| `CODEX_REASONING_EFFORT` | `low` | minimal / low / medium / high / xhigh |
| `CODEX_SANDBOX` | `read-only` | sandbox policy |
| `CODEX_TIMEOUT` | `300` | per codex-call seconds |
| `CODEX_BIN` | `codex` | path to the codex binary |

### Execution-guided self-fix (optional)
With `CODEX_SQL_FIX=1`, after generating, the agent runs its own SQL against the
live MySQL database; if it **fails to execute**, it feeds *only the database error*
back to Codex and asks for a corrected query, looping up to `CODEX_FIX_ATTEMPTS`
times. It is **gold-blind** — it never sees the correct/expected rows, only whether
its own query errors — and stops as soon as the query executes (rows may still be
wrong). This fixes syntax/execution errors, not semantic correctness. Execution is
read-only guarded (refuses non-SELECT statements) and time-capped.

| var | default | meaning |
|-----|---------|---------|
| `CODEX_SQL_FIX` | `0` | `1` to enable the execute-and-fix loop |
| `CODEX_FIX_ATTEMPTS` | `2` | max correction rounds on execution error |
| `CODEX_FIX_TIMEOUT_MS` | `15000` | SELECT execution cap during the fix check |

```bash
CODEX_SQL_FIX=1 CODEX_REASONING_EFFORT=high ./run.sh --dataset dw --setting 1
```
Needs the dataset's MySQL DB loaded + `MYSQL_*` creds (read from env or nearest `.env`).

### Execution-guided explore / decompose / review (optional, gold-blind)
| var | default | meaning |
|-----|---------|---------|
| `CODEX_SQL_EXPLORE` | `0` | run read-only queries against the real tables, inspect the rows *its own* queries return, self-check, then finalize |
| `CODEX_EXPLORE_STEPS` | `4` | max exploratory query rounds |
| `CODEX_EXPLORE_ROWS` | `20` | rows returned per exploratory query |
| `CODEX_DECOMPOSE` | `0` | prepend guidance to self-decompose and validate each sub-step with SQL |
| `CODEX_REVIEW` | `0` | final subagent pass that reviews the answer vs the question for intent capture |

All are gold-blind (DB access is mediated read-only by this process; the model
never sees gold rows). Compose freely, e.g. explore + final fix:
```bash
CODEX_SQL_EXPLORE=1 CODEX_SQL_FIX=1 CODEX_REASONING_EFFORT=high ./run.sh --dataset dw --setting 2
```

> **Note (see RESULTS.md):** `explore/verify` helps (+3–4); but `CODEX_DECOMPOSE`
> and `CODEX_REVIEW` are **net-negative on `dw`** (self-decompose −5, subagent
> review −7 vs baseline) — a documented negative result, off by default. The
> best config is the simple one: hints + explore + fix.

## Files
| file | role | edit? |
|------|------|-------|
| `agent.py`   | Codex-backed `run_agent` (the seam) | swap backend here |
| `prompt.py`  | builds per-question context + hints per `--setting` | rarely |
| `execute.py` | runs the agent in parallel (default 4 workers), resume support | no |
| `unify.py`   | reshapes output into `unified-output/myagent/<run>/{generated,gold}` | no |
| `run.sh`     | CLI wrapper: execute → unify | no |

## Run
```bash
cd eval/myagent
./run.sh --dataset dw --setting 1            # setting 1/2 need no retrieval
# ./run.sh --dataset dw --setting 0          # setting 0 needs retrieve/retrieve.py first
# CODEX_MODEL=gpt-5-codex ./run.sh --dataset dw --setting 1
```

## Score (from `eval/`)
```bash
RUN=<run name printed by run.sh>
uv run python evaluate_ex_acc.py   --dataset dw --input_dir unified-output/myagent/$RUN
uv run python evaluate_subtasks.py --dataset dw --model gpt-5-mini --input_dir unified-output/myagent/$RUN
```
`evaluate_ex_acc.py` needs MySQL loaded + creds in `.env`. `evaluate_subtasks.py`
needs an LLM key (its `--model` is the *grader*, unrelated to the Codex backend).

> Each `codex exec` runs ~30 s at `low` effort; keep `--num_workers` modest to
> avoid Codex rate limits. The full 100-question `dev_sampled` run is sized accordingly.
