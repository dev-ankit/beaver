import json
import argparse
import pandas as pd
from tqdm import tqdm
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv('../.env')

from utils.ex_acc import get_mysql_credentials, execute_sql_with_timeout, compare_results
from utils.utils import write_json

CANDIDATE_SEP = "-- ===CANDIDATE=== --"


def main():
    parser = argparse.ArgumentParser(description="Unified evaluation script for text-to-SQL baselines")
    parser.add_argument("--dataset", type=str, required=True, help="Dataset name for MySQL credentials, e.g. dw")
    parser.add_argument("--input_dir", type=str, required=True, help="Path to the unified output directory containing generated/ and gold/ subdirectories")
    parser.add_argument("--multi", action="store_true",
                        help="Generated files may hold several candidate queries joined by "
                             f"'{CANDIDATE_SEP}'; score a question correct if ANY candidate matches "
                             "(also reports candidate-1-only accuracy)")
    args = parser.parse_args()
    
    generated_dir =  Path(args.input_dir) / "generated"
    gold_dir = Path(args.input_dir) / "gold"
    
    if not generated_dir.exists() or not gold_dir.exists():
        print(f"Error: Could not find 'generated' or 'gold' directories inside {args.input_dir}")
        sys.exit(1)
        
    mysql_creds = get_mysql_credentials(args.dataset)
    if not mysql_creds:
        print(f"Error: Could not load MySQL credentials for dataset {args.dataset}")
        sys.exit(1)
        
    gold_files = sorted(gold_dir.glob("*.sql"))
    
    total_queries = len(gold_files)
    total_attempted = total_queries
    
    total_score = 0
    total_first_match = 0
    nonempty_gold_total = 0
    nonempty_gold_score = 0
    
    results = []
    
    print(f"Evaluating {total_queries} queries from {args.input_dir}...")
    
    for gold_sql_path in tqdm(gold_files):
        filename = gold_sql_path.name
        pred_sql_path = generated_dir / filename
        
        # 1. Execute Gold SQL
        with open(gold_sql_path, "r") as f:
            gold_sql = f.read().strip()
        
        gold_df, gold_err = execute_sql_with_timeout(gold_sql, mysql_creds)
        if gold_df is None:
            gold_df = pd.DataFrame() # Treat error as empty for comparison? 
            # Actually, usually if gold fails, it's a gold_execution_failed
            
        # 2. Execute Predicted SQL
        pred_sql = ""
        if pred_sql_path.exists():
            with open(pred_sql_path, "r") as f:
                pred_sql = f.read().strip()
        
        candidates = [pred_sql]
        if args.multi and pred_sql:
            candidates = [c.strip() for c in pred_sql.split(CANDIDATE_SEP) if c.strip()] or [""]

        # Score each candidate; question is correct if any candidate matches.
        match, msg, pred_err, matched_idx = False, "No prediction", None, None
        first_match = False
        pred_empty = True
        executed = False
        for idx, cand in enumerate(candidates):
            if not cand:
                continue
            executed = True
            cand_df, cand_err = execute_sql_with_timeout(cand, mysql_creds)
            if cand_df is None:
                cand_df = pd.DataFrame()
            cand_match, cand_msg = compare_results(cand_df, gold_df)
            if idx == 0:
                # candidate 1 = the model's best guess; report its stats as primary
                msg, pred_err, pred_empty, first_match = cand_msg, cand_err, cand_df.empty, cand_match
            if cand_match:
                match, matched_idx = True, idx + 1
                if idx > 0:
                    msg = f"Match via candidate {idx + 1} (candidate 1: {msg})"
                break
        if not executed:
            # No prediction at all: compare an empty result set against gold so an
            # empty prediction still scores 1 iff gold is also empty. This matches
            # the original scorer's "both empty -> match" semantics; without it the
            # --multi refactor would silently zero every empty-pred/empty-gold case
            # for all baselines scored by this shared script.
            match, msg = compare_results(pd.DataFrame(), gold_df)
            first_match = match
        score = 1 if match else 0
        
        total_score += score
        total_first_match += 1 if first_match else 0
        gold_is_empty = gold_df.empty

        if not gold_is_empty:
            nonempty_gold_total += 1
            nonempty_gold_score += score

        entry = {
            "file": filename,
            "match": match,
            "score": score,
            "message": msg,
            "gold_empty": gold_is_empty,
            "pred_empty": pred_empty,
            "gold_error": gold_err,
            "pred_error": pred_err
        }
        if args.multi:
            entry["n_candidates"] = len(candidates)
            entry["matched_candidate"] = matched_idx
            entry["candidate1_match"] = first_match
        results.append(entry)
    
    acc_including_empty = (100 * total_score / total_attempted) if total_attempted > 0 else 0.0
    if nonempty_gold_total > 0:
        acc_excluding_empty = 100 * nonempty_gold_score / nonempty_gold_total
    
    # Save results summary
    metrics = {
        "total_evaluated": total_attempted,
        "exact_matches": total_score,
        "accuracy_including_empty": acc_including_empty,
        "nonempty_gold_total": nonempty_gold_total,
        "nonempty_gold_score": nonempty_gold_score,
        "accuracy_excluding_empty": acc_excluding_empty if nonempty_gold_total > 0 else None
    }
    if args.multi:
        metrics["candidate1_matches"] = total_first_match
        metrics["candidate1_accuracy"] = (100 * total_first_match / total_attempted) if total_attempted else 0.0
    summary_data = {
        "metrics": metrics,
        "details": sorted(results, key=lambda x: x["file"])
    }
    
    summary_path = Path(args.input_dir) / "summary_ex_acc.json"
    write_json(summary_data, summary_path)
    
    print(f"\nEvaluation Complete.")
    print(f"Summary saved to: {summary_path}")
    print(json.dumps(summary_data['metrics'], indent=2))

if __name__ == "__main__":
    main()
