#!/bin/bash
set -e

# ============================================================================
# Claude Code (`claude -p`) generation pipeline for BEAVER
# Usage: ./run.sh --dataset dw --setting 1
#
#   Step 1 (execute.py): run `claude -p` over every question (agent.py)
#   Step 2 (unify.py):   reshape outputs into unified-output/claudeagent/<run>/
#
# Score from eval/ with evaluate_ex_acc.py / evaluate_subtasks.py.
# ============================================================================

MODEL="claude"
DATASET="dw"
SETTING=0
NUM_WORKERS=4
Q_FN="dev_sampled"
RESUME_DIR=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --model)       MODEL="$2";       shift 2 ;;
        --dataset)     DATASET="$2";     shift 2 ;;
        --setting)     SETTING="$2";     shift 2 ;;
        --num_workers) NUM_WORKERS="$2"; shift 2 ;;
        --q_fn)        Q_FN="$2";        shift 2 ;;
        --resume)      RESUME_DIR="$2";  shift 2 ;;
        --help|-h)
            echo "Usage: ./run.sh --dataset <ds> --setting <0|1|2> [--num_workers N] [--q_fn FILE] [--resume DIR]"
            echo "  --model        run label for the output dir (default: claude); does NOT select the"
            echo "                 model — set CLAUDE_MODEL to pick the actual model."
            echo "  --dataset      dw | dw_real | neutron | nova (default: dw)"
            echo "  --setting      0=no hints, 1=schema hints, 2=all hints (default: 0)"
            echo "  --num_workers  parallel claude -p calls (default: 4)"
            echo "  --q_fn         question file stem (default: dev_sampled; use dev_one for a 1-eval)"
            echo "  --resume       reuse an existing output dir; execute.py then skips questions that"
            echo "                 already have a prediction."
            echo ""
            echo "  Env (optional): CLAUDE_MODEL, CLAUDE_TIMEOUT (default 300)."
            exit 0
            ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

case "$SETTING" in
    0|1|2) ;;
    *) echo "Invalid --setting: '$SETTING' (expected 0, 1, or 2)"; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVAL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DATA_DIR="$(cd "${SCRIPT_DIR}/../../data" && pwd)"

if [ -f "${EVAL_DIR}/../.env" ]; then
    set -a; source "${EVAL_DIR}/../.env"; set +a
fi

HINTS=""
if [ "$SETTING" -eq 1 ]; then
    HINTS="--gold_tables --mapping --join_keys"
elif [ "$SETTING" -eq 2 ]; then
    HINTS="--gold_tables --mapping --join_keys --knowledge --decomp"
fi

# Resume into an existing dir if asked, else mint a fresh timestamped one.
if [ -n "$RESUME_DIR" ]; then
    OUTPUT_DIR="$RESUME_DIR"
else
    TIMESTAMP=$(date +"%Y%m%d-%H%M%S")
    OUTPUT_DIR="${SCRIPT_DIR}/output/${MODEL}-beaver-${DATASET}-setting${SETTING}-log-${TIMESTAMP}"
fi
mkdir -p "$OUTPUT_DIR"

echo "================================"
echo "ClaudeAgent (claude -p) on BEAVER ${DATASET} (setting ${SETTING}, q_fn ${Q_FN})"
echo "Output Path: $OUTPUT_DIR"
echo "================================"

echo ""
echo "Step 1: Generating Predictions"
echo "=============================="
cd "$SCRIPT_DIR"
uv run python execute.py \
    --model "$MODEL" \
    --dataset "$DATASET" \
    --data_dir "$DATA_DIR" \
    --output_dir "$OUTPUT_DIR" \
    --q_fn "$Q_FN" \
    --num_workers "$NUM_WORKERS" \
    $HINTS

echo ""
echo "Step 2: Unify Predictions"
echo "=============================="
uv run python unify.py \
    --input_dir "$OUTPUT_DIR" \
    --gold_file "${DATA_DIR}/${DATASET}/${Q_FN}.json" \
    --dataset "$DATASET"

echo "========================================"
echo "ClaudeAgent generation complete."
echo "Run name: $(basename "$OUTPUT_DIR")"
echo "Now score it from eval/ with evaluate_ex_acc.py / evaluate_subtasks.py."
