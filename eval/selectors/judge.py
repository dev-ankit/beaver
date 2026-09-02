"""Gold-blind LLM-judge selector: for each question, execute the candidates,
show the judge (claude -p) the question + each candidate's SQL + a preview of
its executed result set, and ask which candidate answers the question. Gold is
never shown to the judge; it is only used afterward to score the judged pick.

Gold-blindness is enforced mechanically, not only by the prompt: the judge
runs with every tool disabled (--tools "") and with the MySQL credentials
stripped from its environment (agent_common.cli_env), so it can neither read
the gold files under unified-output/*/gold/ nor query the database.

Usage (from eval/): python selectors/judge.py <run_dir> [conservative]
Needs the claude CLI on PATH (or CLAUDE_BIN); CLAUDE_MODEL selects the model.
The dataset (dw_real, dw, nova, neutron) is read from the run directory name.
Writes <run_dir>/summary_judge.json (or summary_judge_conservative.json).
Exits 2 if the judge failed on any question (candidate 1 is kept there).
"""
import os
import re
import sys
import json
import shutil
import subprocess
from _common import run_dir, candidates, dev_questions
from utils.ex_acc import get_mysql_credentials, execute_sql_with_timeout, compare_results
from agent_common import cli_env

CONSERVATIVE = len(sys.argv) > 2 and sys.argv[2] == "conservative"
SUMMARY_NAME = "summary_judge_conservative.json" if CONSERVATIVE else "summary_judge.json"
JUDGE_TIMEOUT = int(os.getenv("JUDGE_TIMEOUT", "180"))


def dataset_of(path):
    m = re.search(r"beaver-(dw_real|dw|nova|neutron)-", path.name)
    return m.group(1) if m else "dw_real"


def _claude_cmd():
    """Portable claude invocation with all tools disabled. CLAUDE_MODEL selects
    the judge model (e.g. claude-sonnet-4-5); default is the CLI's default."""
    cmd = [os.getenv("CLAUDE_BIN") or shutil.which("claude") or "claude",
           "-p", "--output-format", "text", "--tools", ""]
    if os.getenv("CLAUDE_MODEL"):
        cmd += ["--model", os.getenv("CLAUDE_MODEL")]
    return cmd


def preview(df, err):
    if err is not None:
        return f"EXECUTION ERROR: {err[:300]}"
    if df is None or df.empty:
        return f"EMPTY RESULT SET (0 rows, columns: {list(df.columns) if df is not None else '?'})"
    head = df.head(8).to_string(max_cols=12)
    if len(head) > 1200:
        head = head[:1200] + "\n...[truncated]"
    return f"shape: {df.shape[0]} rows x {df.shape[1]} cols\n{head}"


def candidate_blocks(cands, execs):
    return [f"CANDIDATE {i+1} SQL:\n{c[:2500]}\n\nCANDIDATE {i+1} EXECUTED RESULT:\n{preview(df, err)}"
            for i, (c, (df, err)) in enumerate(zip(cands, execs))]


def ask_judge(question, blocks):
    """Returns (index of the chosen candidate, raw reply, error or None).
    On any failure (CLI error, timeout, reply that is not a single digit) the
    index is 0 (candidate 1) and error says why; callers must count these."""
    prompt = (
        "You are judging SQL query candidates for a data-warehouse question. "
        "Do not use any tools; answer only from the information below.\n\n"
        f"QUESTION:\n{question}\n\n"
        + "\n\n".join(blocks)
        + "\n\nWhich candidate's executed result correctly answers the question? "
        "Consider whether the result shape, columns, and values are plausible for "
        "what was asked (an empty or error result is rarely correct if others "
        "returned sensible rows)."
        + (" Candidate 1 is the primary answer from a strong model: keep it unless "
           "you are CLEARLY convinced its result is wrong AND another candidate's "
           "result is right — when in doubt, answer 1."
           if CONSERVATIVE else "")
        + " Reply with EXACTLY one character: 1, 2, or 3."
    )
    try:
        proc = subprocess.run(
            _claude_cmd(), input=prompt, capture_output=True, text=True,
            encoding="utf-8", timeout=JUDGE_TIMEOUT, env=cli_env(),
        )
    except subprocess.TimeoutExpired:
        return 0, "", f"judge timeout after {JUDGE_TIMEOUT}s"
    except OSError as e:
        return 0, "", f"judge launch error: {e}"
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return 0, raw[:80], f"judge exit {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
    m = re.fullmatch(r"[^\d]{0,3}([1-9])[^\d]{0,3}", raw)
    if not m:
        return 0, raw[:80], f"judge reply not a single digit: {raw[:60]!r}"
    return int(m.group(1)) - 1, raw[:80], None


def main():
    rd = run_dir(sys.argv[1])
    dataset = dataset_of(rd)
    creds = get_mysql_credentials(dataset)
    questions = {qid: r["question"] for qid, r in dev_questions(dataset).items()}
    import pandas as pd
    n_total = n_sel = n_c1 = n_any = 0
    errors = []
    details = []
    for gf in sorted((rd / "generated").glob("*.sql")):
        qid = gf.stem
        cands = candidates(gf.read_text(encoding="utf-8"))
        execs = [execute_sql_with_timeout(c, creds) for c in cands]
        sel_idx, raw, err = ask_judge(questions.get(qid, ""), candidate_blocks(cands, execs))
        if err:
            errors.append(qid)
        if sel_idx >= len(cands):
            sel_idx = 0

        gold_df, _ = execute_sql_with_timeout((rd / "gold" / f"{qid}.sql").read_text(encoding="utf-8").strip(), creds)
        if gold_df is None:
            gold_df = pd.DataFrame()

        def match(i):
            df = execs[i][0]
            return bool(compare_results(df if df is not None else pd.DataFrame(), gold_df)[0])

        sel_m, c1_m = match(sel_idx), match(0)
        any_m = any(match(i) for i in range(len(cands)))
        n_total += 1; n_sel += sel_m; n_c1 += c1_m; n_any += any_m
        details.append({"id": qid, "judged": sel_idx + 1, "judged_match": sel_m,
                        "candidate1_match": c1_m, "any_match": any_m,
                        "judge_raw": raw, "judge_error": err})
        print(f"{qid}: judged c{sel_idx+1} match={sel_m} (c1={c1_m} any={any_m})"
              + (f"  ERROR: {err}" if err else ""), flush=True)

    summary = {
        "total": n_total,
        "candidate1_accuracy": round(100 * n_c1 / n_total, 2),
        "judged_accuracy": round(100 * n_sel / n_total, 2),
        "pass_at_3_accuracy": round(100 * n_any / n_total, 2),
        "judge_errors": len(errors),
    }
    (rd / SUMMARY_NAME).write_text(
        json.dumps({"metrics": summary, "details": details}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if errors:
        print(f"JUDGE ERRORS on {len(errors)} questions (candidate 1 kept there): {errors}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
