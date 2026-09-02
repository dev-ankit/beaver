"""Reshape raw per-instance outputs into the unified layout the scorers read:

    eval/unified-output/<method>/<run_name>/
    ├── generated/<id>.sql   (your agent's prediction)
    └── gold/<id>.sql        (gold SQL from the question file)

<method> is this folder's name. For each id-subdir under --input_dir it grabs
result.sql, or the first *.sql it finds (here: predicted_0.sql). Generic — you
should not need to edit this.
"""
import os
import sys
import json
import argparse
import glob
import re

from tqdm import tqdm

eval_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(eval_dir)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_dir", type=str, required=True, help="Directory containing generated outputs")
    parser.add_argument("--gold_file", type=str, required=True, help="Path to dev.json or dev_sampled.json")
    parser.add_argument("--dataset", type=str, required=True, help="Dataset name, e.g. dw")
    args = parser.parse_args()

    args.input_dir = args.input_dir.rstrip('/')
    run_name = os.path.basename(args.input_dir)

    baseline_name = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
    unified_dir = os.path.join(eval_dir, "unified-output", baseline_name, run_name)
    generated_dir = os.path.join(unified_dir, "generated")
    gold_dir = os.path.join(unified_dir, "gold")
    os.makedirs(generated_dir, exist_ok=True)
    os.makedirs(gold_dir, exist_ok=True)

    with open(args.gold_file, 'r', encoding="utf-8") as f:
        gold_data = json.load(f)
    id_to_entry = {entry['id']: entry for entry in gold_data}

    subdirs = sorted(d for d in glob.glob(os.path.join(args.input_dir, "*")) if os.path.isdir(d))
    print(f"Processing {len(subdirs)} subdirectories for {baseline_name}...")
    for subdir_path in tqdm(subdirs):
        subdir_name = os.path.basename(subdir_path)

        if subdir_name in id_to_entry:
            gold_entry = id_to_entry[subdir_name]
        else:
            # Fallback for index-based dir names (e.g. ..._001)
            match = re.search(r'_(\d+)$', subdir_name)
            if match and int(match.group(1)) < len(gold_data):
                gold_entry = gold_data[int(match.group(1))]
            else:
                continue

        real_id = gold_entry['id']
        gold_sql = gold_entry.get("sql", gold_entry.get("oracle_sql", gold_entry.get("gold_sql", "")))

        with open(os.path.join(gold_dir, f"{real_id}.sql"), "w", encoding="utf-8") as f:
            f.write(gold_sql)

        result_sql_path = os.path.join(subdir_path, "result.sql")
        if not os.path.exists(result_sql_path):
            sql_files = glob.glob(os.path.join(subdir_path, "*.sql"))
            if sql_files:
                result_sql_path = sql_files[0]

        pred_sql = ""
        if os.path.exists(result_sql_path):
            with open(result_sql_path, "r", encoding="utf-8") as f:
                pred_sql = f.read().strip()

        with open(os.path.join(generated_dir, f"{real_id}.sql"), "w", encoding="utf-8") as f:
            f.write(pred_sql)

    print(f"Saved SQL files to {unified_dir}")


if __name__ == "__main__":
    main()
