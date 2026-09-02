"""Gold-blind LLM-judge selector: for each question, execute the 3 candidates,
show the judge (claude -p) the question + each candidate's SQL + a preview of
its executed result set, and ask which candidate answers the question. Gold is
never shown to the judge; it is only used afterward to score the judged pick.

Usage: python judge.py <run_dir> [conservative]   (needs the claude CLI on PATH; CLAUDE_MODEL selects the model)
"""
import sys
import json
import subprocess
import re
from pathlib import Path
from _common import run_dir, candidates, dev_questions, CANDIDATE_SEP
from utils.ex_acc import get_mysql_credentials, execute_sql_with_timeout, compare_results

import os
import shutil

def _claude_cmd():
    """Portable claude invocation; CLAUDE_MODEL selects the judge model
    (e.g. claude-sonnet-4-5), default is the CLI's default (Opus 5)."""
    cmd = [os.getenv("CLAUDE_BIN") or shutil.which("claude") or "claude", "-p", "--output-format", "text"]
    if os.getenv("CLAUDE_MODEL"):
        cmd += ["--model", os.getenv("CLAUDE_MODEL")]
    return cmd

CONSERVATIVE = len(sys.argv) > 2 and sys.argv[2] == "conservative"
SUMMARY_NAME = "summary_judge_conservative.json" if CONSERVATIVE else "summary_judge.json"

def preview(df, err):
    if err is not None:
        return f"EXECUTION ERROR: {err[:300]}"
    if df is None or df.empty:
        return f"EMPTY RESULT SET (0 rows, columns: {list(df.columns) if df is not None else '?'})"
    head = df.head(8).to_string(max_cols=12)
    if len(head) > 1200:
        head = head[:1200] + "\n...[truncated]"
    return f"shape: {df.shape[0]} rows x {df.shape[1]} cols\n{head}"

def ask_judge(question, blocks):
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
            _claude_cmd(),
            input=prompt, capture_output=True, text=True, encoding="utf-8",
            timeout=180,
        )
        m = re.search(r"[123]", (proc.stdout or "").strip()[:20])
        return (int(m.group(0)) - 1) if m else 0, (proc.stdout or "").strip()[:80]
    except Exception as e:
        return 0, f"judge error: {e}"

def main():
    run_dir = run_dir(sys.argv[1])
    creds = get_mysql_credentials("dw_real")
    import pandas as pd
    n_total = n_sel = n_c1 = n_any = 0
    details = []
    for gf in sorted((run_dir / "generated").glob("*.sql")):
        qid = gf.stem
        cands = [c.strip() for c in gf.read_text(encoding="utf-8").split(CANDIDATE_SEP) if c.strip()] or [""]
        record = QUESTIONS.get(qid, "")
        execs = [execute_sql_with_timeout(c, creds) for c in cands]
        blocks = [
            f"CANDIDATE {i+1} SQL:\n{c[:2500]}\n\nCANDIDATE {i+1} EXECUTED RESULT:\n{preview(df, err)}"
            for i, (c, (df, err)) in enumerate(zip(cands, execs))
        ]
        sel_idx, raw = ask_judge(record, blocks)
        if sel_idx >= len(cands):
            sel_idx = 0

        gold_sql = (run_dir / "gold" / f"{qid}.sql").read_text(encoding="utf-8").strip()
        gold_df, _ = execute_sql_with_timeout(gold_sql, creds)
        if gold_df is None:
            gold_df = pd.DataFrame()

        def match(i):
            df = execs[i][0]
            if df is None:
                df = pd.DataFrame()
            return bool(compare_results(df, gold_df)[0])

        sel_m, c1_m = match(sel_idx), match(0)
        any_m = any(match(i) for i in range(len(cands)))
        n_total += 1; n_sel += sel_m; n_c1 += c1_m; n_any += any_m
        details.append({"id": qid, "judged": sel_idx + 1, "judged_match": sel_m,
                        "candidate1_match": c1_m, "any_match": any_m, "judge_raw": raw})
        print(f"{qid}: judged c{sel_idx+1} match={sel_m} (c1={c1_m} any={any_m})", flush=True)

    summary = {
        "total": n_total,
        "candidate1_accuracy": round(100 * n_c1 / n_total, 2),
        "judged_accuracy": round(100 * n_sel / n_total, 2),
        "pass_at_3_accuracy": round(100 * n_any / n_total, 2),
    }
    (run_dir / SUMMARY_NAME).write_text(
        json.dumps({"metrics": summary, "details": details}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))

QUESTIONS = {}
def load_questions():
    for qid, r in dev_questions().items():
        QUESTIONS[qid] = r["question"]

if __name__ == "__main__":
    load_questions()
    main()
