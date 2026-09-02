#!/bin/bash
# Concur end to end: generator (Codex CLI, 3 candidates) -> selector's own
# query (Claude CLI) -> selection -> scores. One command reproduces a row of
# RESULTS_dw_real.md. Needs the MySQL database (case-insensitive, see the
# results file), a logged-in `codex` and `claude` CLI, and `uv`.
#
# Usage (from eval/):
#   ./concur.sh --dataset dw_real --setting 2
#   ./concur.sh --dataset dw --setting 2            # 100-question dev_sampled
# Options: --q_fn <file stem> (default dev for dw_real, dev_sampled otherwise),
#          --num_workers N (default 2).
set -e
DATASET=dw_real; SETTING=2; Q_FN=""; NW=2
while [[ $# -gt 0 ]]; do
    case $1 in
        --dataset)     DATASET="$2"; shift 2 ;;
        --setting)     SETTING="$2"; shift 2 ;;
        --q_fn)        Q_FN="$2";    shift 2 ;;
        --num_workers) NW="$2";      shift 2 ;;
        *) echo "Usage: ./concur.sh --dataset <dw|dw_real|nova|neutron> --setting <1|2> [--q_fn FILE] [--num_workers N]"; exit 1 ;;
    esac
done
if [ -z "$Q_FN" ]; then
    if [ "$DATASET" = "dw_real" ]; then Q_FN=dev; else Q_FN=dev_sampled; fi
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== Stage 1: generator (Codex CLI, 3 candidates per question)"
CODEX_RUN=$(cd "$HERE/myagent" && \
    CODEX_SQL_FIX=1 CODEX_SQL_EXPLORE=1 CODEX_STYLE_GUIDE=1 CODEX_SCHEMA_SKILL=1 \
    CODEX_N_CANDIDATES=3 CODEX_REASONING_EFFORT=high \
    ./run.sh --dataset "$DATASET" --setting "$SETTING" --q_fn "$Q_FN" --num_workers "$NW" \
    | tee /dev/stderr | sed -n 's/^Run name: //p')
[ -n "$CODEX_RUN" ] || { echo "generator run did not report a run name"; exit 1; }

echo "== Stage 2a: selector's own query (Claude CLI)"
CLAUDE_RUN=$(cd "$HERE/claudeagent" && \
    CLAUDE_SQL_FIX=1 CLAUDE_SQL_EXPLORE=1 CLAUDE_EFFORT=high \
    ./run.sh --dataset "$DATASET" --setting "$SETTING" --q_fn "$Q_FN" --num_workers "$NW" \
    | tee /dev/stderr | sed -n 's/^Run name: //p')
[ -n "$CLAUDE_RUN" ] || { echo "claude run did not report a run name"; exit 1; }

cd "$HERE"
echo "== Stage 2b: selection (cross-model concurrence, then judge)"
uv run python selectors/concur.py "unified-output/myagent/$CODEX_RUN" "unified-output/claudeagent/$CLAUDE_RUN"
CONCUR_RUN="${CODEX_RUN/codex-/concur-}"

echo "== Generator scores: pass@1 = candidate1_accuracy, pass@3 = accuracy_including_empty"
uv run python evaluate_ex_acc.py --dataset "$DATASET" --multi --input_dir "unified-output/myagent/$CODEX_RUN" | tail -10
echo "== Concur score: pass@1 = accuracy_including_empty"
uv run python evaluate_ex_acc.py --dataset "$DATASET" --input_dir "unified-output/concur/$CONCUR_RUN" | tail -8
echo "runs: unified-output/myagent/$CODEX_RUN  unified-output/claudeagent/$CLAUDE_RUN  unified-output/concur/$CONCUR_RUN"
