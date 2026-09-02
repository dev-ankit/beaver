import json
import pandas as pd
from dataclasses import dataclass


def read_json(fn):
    with open(fn, encoding="utf-8") as f:
        return json.load(f)


def write_json(obj, fn):
    with open(fn, 'w', encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def format_join(join_pair):
    """Format a [left, right] join pair for display."""
    left, right = join_pair
    return f"{left.split('.')[0]} joins {right.split('.')[0]} on {left} = {right}"


def format_table(table_name, corpus_tables, use_instance: bool, corpus_markdowns=None, trim=False):
    rows = corpus_tables[table_name]["example_rows"]
    if trim:
        for row in rows:
            for i in range(len(row)):
                if isinstance(row[i], str):
                    row[i] = row[i][:500]

    cols = corpus_tables[table_name]["column_names"]

    if corpus_markdowns is None:
        df = pd.DataFrame(rows, columns=cols)
        df_md = df.to_markdown(index=False)
    else:
        df_md = corpus_markdowns[table_name]

    table_string = [
        f'Table name: {table_name}',
        f"Example table content:\n{df_md}",
    ]

    if use_instance:
        instances = corpus_tables[table_name]["example_columns"]
        table_string.append("Top-10 most occurring values for each column:")
        for col_idx, col_name in enumerate(cols):
            _instance = " | ".join([str(x) for x in instances[col_idx]])
            table_string.append(f"{col_name}: {_instance}")

    return "\n".join(table_string)


def format_tables(tables, corpus_tables, use_instance, corpus_markdowns=None):
    return "\n\n".join(
        format_table(t, corpus_tables, use_instance, corpus_markdowns) for t in tables
    )


@dataclass
class EvalConfig:
    gold_tables: bool
    join_keys: bool
    mapping: bool
    knowledge: bool
    decomp: bool
    instances: bool = False


def system(content: str):
    return {"role": "system", "content": content}


def user(content: str):
    return {"role": "user", "content": content}


def assistant(content: str):
    return {"role": "assistant", "content": content}
