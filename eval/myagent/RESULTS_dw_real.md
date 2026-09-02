# Concur on BEAVER: results on `dw` and `dw_real`

Concur is a two-stage text-to-SQL method built on the harness in this
directory. Stage 1 is the generator (`myagent/agent.py`, Codex CLI, model
`gpt-5.6-sol`): it explores the schema with its own queries, runs its SQL and
repairs execution errors, follows the nine-rule style guide, and writes three
candidate queries per question (its best reading, plus two alternatives that
flip the choices the question leaves open, such as COUNT versus
COUNT(DISTINCT)). Stage 2 is the selector (`selectors/concur.py`, Claude CLI,
model `claude-opus-5`): it writes its own query for each question; where its
result matches a candidate, that candidate is chosen, otherwise a judge picks
among the candidates from their executed results. Claude's own query is never
returned. Everything is gold-blind.

Question sets: `dw` is the 100-question `dev_sampled` set; `dw_real` is the
121 real MIT warehouse queries in `dw_real/dev.json`. Settings 1 and 2 follow
the paper's protocol (schema-linking hints; all five hints). All runs on a
case-insensitive MySQL (`--lower-case-table-names=1`, see below).

## Metrics

pass@1: the single returned query's result matches gold (the paper's metric).
pass@3: at least one of the generator's three candidates matches gold. It is
reported only for multi-candidate runs; Concur returns one query and has only
a pass@1.

## Results

| Setting | Config | dw pass@1 | dw pass@3 | dw_real pass@1 | dw_real pass@3 |
|:---:|---|:---:|:---:|:---:|:---:|
| 1 | Paper, ReFoRCE* | 18.9 | not reported | 18.9 | not reported |
| 1 | Concur (generator + selector), GPT-5.6 + Opus 5 | 28.0 | n.a. | 29.8 | n.a. |
| 2 | Paper, ReFoRCE* | 25.9 | not reported | 25.9 | not reported |
| 2 | Paper, Few-shot* | 23.1 | not reported | 23.1 | not reported |
| 2 | GPT-5.5, one query, repair only | 30 | n.a. | not run | n.a. |
| 2 | Concur's generator, GPT-5.5 | 39 | 49 | not run | not run |
| 2 | Plain GPT-5.6, one query, no techniques | 38.0 | n.a. | 35.5 | n.a. |
| 2 | Claude Opus 5, one query, exploration and repair | 39.0 | n.a. | 30.6 | n.a. |
| 2 | Concur's generator, GPT-5.6 | 32 | 47 | 29.8 | 47.9 |
| 2 | Concur (generator + selector), GPT-5.6 + Opus 5 | 38.0 | n.a. | 38.0 | n.a. |

*Paper rows (arXiv 2409.02038v3, Table 6) are the whole benchmark, all three
warehouses, averaged over seven models; the paper reports no per-question-set
numbers at Settings 1 and 2. The GPT-5.5 rows are from `RESULTS.md`; its
GPT-5.6 rerun of the same generator config scored 33 / 49, one seed apart from
the 32 / 47 run used here. At Setting 1 on `dw` the generator scores 25 / 34
and Claude Opus 5 with exploration and repair 31.0.

The judge stage is a model call with no temperature control, so its picks
vary between runs. Three independent judge passes on the same `dw_real`
Setting 2 candidates gave Concur 36.4, 40.5, and 37.2 (mean 38.0, which is
the number in the table); the `dw` cells are single passes. Cross-model
concurrence, which decides 42% of the questions, is deterministic.

The experiments run on different model generations to decouple technique from
model improvements. On `dw` the generator's techniques lift GPT-5.5 from 30 to
39 pass@1, and plain GPT-5.6 with no techniques lands at 38. On GPT-5.6 the
generator's first candidate scores below the plain model on both sets, but
one of its three candidates matches gold on 49 (`dw`) and 47.9% (`dw_real`) of
questions, and on `dw_real` the selector turns that into 38.0 pass@1.

## Database requirement

15 of the 121 `dw_real` gold queries depend on case-insensitive identifiers:
12 use lowercase table names (`dw.employee_directory`, ...) and 3
(dw_real_20/49/57) refer to a table alias in a different case than declared.
MySQL on macOS runs them; a default Linux or Windows server errors, and the
scorer then treats gold as empty. Build the database with
`--lower-case-table-names=1` (`selectors/ops/rebuild_db.sh`) and check with
`python selectors/gold_audit.py <run>` (expect 0 gold errors). A further 14
gold queries return something the question did not ask (for example
dw_real_77 sums a building's area over 60 joined rows; dw_real_44 groups by
course name but does not return it); they are scored as released.

## Reproduce

One command, from `eval/`, runs both stages and prints the generator's
pass@1 and pass@3 and Concur's pass@1 (needs the database, logged-in
`codex` and `claude` CLIs, and `uv`):

```bash
./concur.sh --dataset dw_real --setting 2
./concur.sh --dataset dw --setting 2
```

The steps it runs, for running them separately. Generator (from
`eval/myagent`; the `dw` best config from `RESULTS.md`):

```bash
CODEX_SQL_FIX=1 CODEX_SQL_EXPLORE=1 CODEX_STYLE_GUIDE=1 CODEX_SCHEMA_SKILL=1 \
CODEX_N_CANDIDATES=3 CODEX_REASONING_EFFORT=high \
  ./run.sh --dataset dw_real --setting 2 --q_fn dev
```

Selector's own query (from `eval/claudeagent`):

```bash
CLAUDE_SQL_FIX=1 CLAUDE_SQL_EXPLORE=1 CLAUDE_EFFORT=high \
  ./run.sh --dataset dw_real --setting 2 --q_fn dev
```

Concur and scoring (from `eval/`; the dataset is read from the run name):

```bash
python selectors/concur.py unified-output/myagent/<codex run> unified-output/claudeagent/<claude run>
python evaluate_ex_acc.py --dataset dw_real --input_dir unified-output/concur/<out>
python evaluate_ex_acc.py --dataset dw_real --multi --input_dir unified-output/myagent/<codex run>
```

`--multi` is required for pass@1/pass@3 on a three-candidate run; without it
the candidates are executed as one string. For `dw` use `--dataset dw` and the
default `--q_fn dev_sampled`. The plain-model rows are the same `run.sh` with
only `CODEX_REASONING_EFFORT=high` set. Setting 1 is `--setting 1`.

## Portability

Prompts go to the CLIs over stdin with explicit UTF-8 (Windows caps argv at
8191 chars and defaults pipes to ANSI; both silently fatal for ~30KB prompts
with non-ASCII). Binaries resolve via `shutil.which`, so bare `codex`/`claude`
work on Windows and macOS alike; `CODEX_BIN`/`CLAUDE_BIN` still override.
`CODEX_GRAIN=1` (with `grain_profile.py`) is an experimental generator option
and is not part of the reported configuration.
