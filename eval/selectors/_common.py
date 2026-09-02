"""Shared setup for the selector/analysis scripts: make eval/ importable from
any cwd, load the repo-root .env, and resolve run directories given either
relative to cwd or relative to eval/."""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # eval/selectors
EVAL = HERE.parent                              # eval/
REPO = EVAL.parent                              # repo root
if str(EVAL) not in sys.path:
    sys.path.insert(0, str(EVAL))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(REPO / ".env")

CANDIDATE_SEP = "-- ===CANDIDATE=== --"


def run_dir(p):
    """Accept a path relative to cwd, relative to eval/, or absolute."""
    p = Path(p)
    for cand in (p, EVAL / p):
        if cand.is_dir():
            return cand.resolve()
    raise SystemExit(f"run dir not found: {p}")


def candidates(sql_text):
    cs = [c.strip() for c in sql_text.split(CANDIDATE_SEP) if c.strip()]
    return cs or [""]


def dev_questions(dataset="dw_real", q_fn="dev"):
    import json
    with open(REPO / "data" / dataset / f"{q_fn}.json", encoding="utf-8") as f:
        return {r["id"]: r for r in json.load(f)}
