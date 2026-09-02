#!/bin/bash
set -e

# ============================================================================
# Custom-agent generation pipeline for BEAVER
# Usage: ./run.sh --dataset dw --setting 1
#
#   Step 1 (execute.py): run your agent (agent.py) over every question
#   Step 2 (unify.py):   reshape outputs into unified-output/myagent/<run>/
#
# Then score with (from eval/):
#   uv run python evaluate_ex_acc.py   --dataset <ds> --input_dir unified-output/myagent/<run>
#   uv run python evaluate_subtasks.py --dataset <ds> --model <label> --input_dir unified-output/myagent/<run>
# ============================================================================

MODEL="codex"
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
            echo "Usage: ./run.sh --dataset <dataset> --setting <0|1|2> [--num_workers N] [--q_fn FILE] [--resume DIR]"
            echo ""
            echo "  --model        run label, used only for the output dir name (default: codex)."
            echo "                 Backend = Codex CLI; set CODEX_MODEL to pick the actual Codex model"
            echo "                 (--model does NOT select the model, only the output-dir label)."
            echo "  --dataset      dw | dw_real | neutron | nova (default: dw)"
            echo "  --setting      0=no hints (retrieved tables), 1=schema hints, 2=all hints (default: 0)"
            echo "  --num_workers  parallel codex exec calls (default: 4)"
            echo "  --q_fn         question file stem (default: dev_sampled; use dev_one for a 1-eval)"
            echo "  --resume       reuse an existing output dir instead of a fresh timestamped one;"
            echo "                 execute.py then skips questions that already have a prediction."
            echo ""
            echo "  Codex env (optional): CODEX_MODEL, CODEX_REASONING_EFFORT (default low),"
            echo "                        CODEX_SANDBOX (default read-only), CODEX_TIMEOUT (default 300)."
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

# --- Load .env (API keys) ---
if [ -f "${EVAL_DIR}/../.env" ]; then
    set -a
    source "${EVAL_DIR}/../.env"
    set +a
fi

# Map --setting to the hint flags execute.py understands.
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
echo "MyAgent on BEAVER ${DATASET} (setting ${SETTING})"
echo "Model: $MODEL"
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
echo "MyAgent generation complete."
echo "Run name: $(basename "$OUTPUT_DIR")"
echo "Now score it from eval/ with evaluate_ex_acc.py / evaluate_subtasks.py."
