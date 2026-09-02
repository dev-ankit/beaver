"""Concur, stage 2: turn a multi-candidate Codex run plus an independent Claude
run into a single-answer run directory that evaluate_ex_acc.py scores like any
other method.

Policy (gold-blind):
  1. Execute Claude's answer and all Codex candidates. If Claude's result set
     matches a Codex candidate, select that candidate (cross-model concurrence;
     lowest index on ties).
  2. Otherwise ask a judge (claude -p) to pick among the Codex candidates from
     their executed result previews. If <codex_run>/summary_judge.json already
     exists (from judge.py), its stored picks are reused; otherwise the judge is
     called here.
  3. Write <out>/generated/<id>.sql (the selected candidate only) and copy gold/.

Usage (from eval/):
  python selectors/concur.py <codex_run_dir> <claude_run_dir> [out_dir]
Then: python evaluate_ex_acc.py --dataset <dataset> --input_dir <out_dir>
The dataset (dw_real, dw, nova, neutron) is read from the codex run directory name.
"""
import sys
import json
import shutil
from pathlib import Path
import pandas as pd
from _common import run_dir, candidates, dev_questions, EVAL
from utils.ex_acc import get_mysql_credentials, execute_sql_with_timeout, compare_results

codex_dir = run_dir(sys.argv[1])
claude_dir = run_dir(sys.argv[2])
out = Path(sys.argv[3]) if len(sys.argv) > 3 else EVAL / "unified-output" / "concur" / codex_dir.name.replace("codex-", "concur-")
(out / "generated").mkdir(parents=True, exist_ok=True)
(out / "gold").mkdir(parents=True, exist_ok=True)

import re
m = re.search(r"beaver-(dw_real|dw|nova|neutron)-", codex_dir.name)
DATASET = m.group(1) if m else "dw_real"
creds = get_mysql_credentials(DATASET)
judge_path = codex_dir / "summary_judge.json"
stored = {x["id"]: x["judged"] for x in json.load(open(judge_path, encoding="utf-8"))["details"]} if judge_path.exists() else {}
questions = dev_questions(DATASET)

if not stored:
    sys.path.insert(0, str(EVAL / "selectors"))
    from judge import ask_judge, preview  # noqa: E402

log = []
for gf in sorted((codex_dir / "generated").glob("*.sql")):
    qid = gf.stem
    cands = candidates(gf.read_text(encoding="utf-8"))
    execs = [execute_sql_with_timeout(c, creds) for c in cands]
    pick, how = 0, "candidate1"
    cl = claude_dir / "generated" / f"{qid}.sql"
    if cl.exists():
        cdf, cerr = execute_sql_with_timeout(cl.read_text(encoding="utf-8").strip(), creds)
        if cdf is not None and cerr is None:
            for k, (df, err) in enumerate(execs):
                if df is not None and err is None and compare_results(df, cdf)[0]:
                    pick, how = k, f"concurrence:c{k+1}"
                    break
    if how == "candidate1" and len(cands) > 1:
        if qid in stored:
            pick, how = min(stored[qid] - 1, len(cands) - 1), f"judge(stored):c{stored[qid]}"
        else:
            blocks = [f"CANDIDATE {i+1} SQL:\n{c[:2500]}\n\nCANDIDATE {i+1} EXECUTED RESULT:\n{preview(df, err)}"
                      for i, (c, (df, err)) in enumerate(zip(cands, execs))]
            j, _ = ask_judge(questions[qid]["question"], blocks)
            pick, how = min(j, len(cands) - 1), f"judge:c{j+1}"
    (out / "generated" / f"{qid}.sql").write_text(cands[pick], encoding="utf-8")
    shutil.copy(codex_dir / "gold" / f"{qid}.sql", out / "gold" / f"{qid}.sql")
    log.append({"id": qid, "selected": pick + 1, "how": how})

json.dump(log, open(out / "concur_selection.json", "w", encoding="utf-8"), indent=2)
from collections import Counter
print(f"wrote {len(log)} single-answer predictions to {out}")
print("selection sources:", dict(Counter(x["how"].split(":")[0] for x in log)))
print(f"score with: python evaluate_ex_acc.py --dataset {DATASET} --input_dir {out.relative_to(EVAL) if out.is_relative_to(EVAL) else out}")
