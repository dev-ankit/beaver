"""Build per-question instances for the agent.

This mirrors the context/hint assembly used by the `fewshot` baseline
(`eval/fewshot/prompt.py`) so the --setting flag behaves identically, but it
returns a list of *aligned* instance dicts instead of two parallel lists. Each
instance carries both:
  * a ready-to-send OpenAI-style chat `prompt` (system + few-shot example + user)
    for agents that just want to call a chat model, and
  * the structured fields (question, db, tables, and the active hint strings) for
    agents that do their own multi-step reasoning / tool use.
"""
from pathlib import Path

from utils import (
    read_json, format_tables, EvalConfig, format_join, system, user, assistant,
)


def get_mapping(mapping):
    desc = []
    for sq in mapping:
        cols_desc = []
        for col in mapping[sq]:
            table_name, col = col.split(".")
            cols_desc.append(f"column {col} in table {table_name.lower()}")
        desc.append(f'"{sq}" in the user question refers to {", ".join(cols_desc)}')
    return "\n".join(desc)


def get_join_keys(join_keys):
    return "\n".join(format_join(jk) for jk in join_keys)


def get_knowledge(domain_knowledge):
    return "\n".join(domain_knowledge)


def get_decomp(decomps):
    return "\n".join(f"Subquery {i + 1}: {sq}" for i, sq in enumerate(decomps))


def get_user_prompt(q, tables, corpus_tables, eval_config: EvalConfig):
    desc = [
        format_tables(tables, corpus_tables, eval_config.instances),
        f"User question: {q['question']}",
    ]
    if eval_config.mapping:
        desc.append(f"Mapping:\n{get_mapping(q['column_mapping'])}")
    if eval_config.join_keys:
        desc.append(f"Join keys:\n{get_join_keys(q['join_keys'])}")
    if eval_config.knowledge and get_knowledge(q['domain_knowledge']) != '':
        desc.append(f"Domain knowledge:\n{get_knowledge(q['domain_knowledge'])}")
    if eval_config.decomp and get_decomp(q["sub_questions"]) != "":
        desc.append(f"Query decomposition:\n{get_decomp(q['sub_questions'])}")
    return user("\n\n".join(desc))


def get_retrieved_tables(dataset: str, data_dir="../../data"):
    retrieved_fn = Path(f"{data_dir}/{dataset}/retrieval/retrieved_tables.json")
    reranked_fn = Path(f"{data_dir}/{dataset}/retrieval/reranked_tables.json")
    if not retrieved_fn.exists():
        raise FileNotFoundError(
            f"No retrieved tables at {retrieved_fn}. At --setting 0 the agent gets the "
            f"retrieved candidate tables; run `python retrieve/retrieve.py --dataset {dataset} ...` "
            f"first, or use --setting 1/2 which inject gold tables and need no retrieval."
        )
    if reranked_fn.exists():
        print(f"Loading reranked tables from {reranked_fn}")
        return read_json(reranked_fn)
    print(f"Loading retrieved tables from {retrieved_fn}")
    return read_json(retrieved_fn)


def _build_instruction(eval_config, q_knowledge, q_decomp, q, structures):
    db_type = "MySQL"
    instruction = ["You are given a list of tables", "a user question"]
    if eval_config.join_keys:
        instruction.append("join keys among the provided tables")
    if eval_config.mapping:
        instruction.append("a mapping from information mentioned in the user question to columns in the provided tables")
    if q_knowledge:
        instruction.append("domain knowledge")
    if q_decomp:
        instruction.append("decomposition of the user question")
    instruction[-1] = f"and {instruction[-1]}"
    instruction = ", ".join(instruction) + ", "

    instruction += (
        f"your task is output a {db_type} SQL statement that can be used to answer the user "
        f"question based on the provided information. You need to ensure that syntax and functions "
        f"used in your SQL statement are appropriate for {db_type} database. If you are unable to "
        f"determine the SQL statement, output None. "
    )
    if eval_config.mapping:
        instruction += "You should use the provided mapping to determine which columns and tables should be used in the SQL statement. "
    if eval_config.join_keys:
        instruction += "You should use the provided join keys to determine how to connect the tables in the SQL statement. "
    if q_knowledge:
        instruction += "You should use the provided domain knowledge to determine which tables, columns, and literals should be used in the SQL statement. "
    if q_decomp:
        instruction += (
            "You must answer each subquery individually and then combine them to form the complete "
            "SQL statement. Each subquery you generate must be explicitly used in the final SQL "
            "statement, without being simplified. "
        )
        instruction += (
            "Below is the structure of the SQL statement with subqueries denoted. Each provided "
            "subquery is used in the final SQL statement in such a structure."
        )
        structure_name = q.get("detailed_category")
        if structure_name and structure_name != 'real' and structure_name in structures:
            structure = structures[structure_name]
            instruction += f"\n\n{structure['structure']}"
            instruction += f"\n\n{structure['subquery_decomposition']} "

    instruction += "The SQL statement need to be wrapped in <ans></ans> tags."
    return instruction


def build_instances(dataset: str, q_fn: str, eval_config: EvalConfig, data_dir: str = "../../data"):
    """Return a list of aligned instance dicts, one per question that has table context.

    Each instance:
      id       : question id
      question : natural-language question
      db       : target database name
      tables   : list of candidate table names provided to the agent
      prompt   : OpenAI-style chat messages (system + few-shot example + user)
      hints    : {mapping, join_keys, domain_knowledge, decomposition} active strings
      record   : full question/gold dict from <q_fn>.json (avoid peeking at gold `sql`)
    """
    structures = read_json(f"{data_dir}/template_structure.json")
    qs = read_json(f"{data_dir}/{dataset}/{q_fn}.json")
    example = read_json(f"{data_dir}/{dataset}/example.json")
    dev_tables = read_json(f"{data_dir}/{dataset}/dev_tables.json")

    retrieved_tables = None if eval_config.gold_tables else get_retrieved_tables(dataset, data_dir)

    example_prompt = [
        get_user_prompt(example, example["tables"], dev_tables, eval_config),
        assistant(f"SQL: <ans>{example['sql']}</ans>"),
    ]

    instances = []
    skipped = 0
    for q in qs:
        q_id = q['id']
        q_knowledge = eval_config.knowledge and get_knowledge(q['domain_knowledge']) != ''
        q_decomp = eval_config.decomp and get_decomp(q["sub_questions"]) != ''

        if eval_config.gold_tables:
            tables = q["tables"]
        else:
            if q_id not in retrieved_tables:
                skipped += 1
                continue
            tables = retrieved_tables[q_id]

        instruction = _build_instruction(eval_config, q_knowledge, q_decomp, q, structures)
        prompt = [system(instruction)] + example_prompt + [get_user_prompt(q, tables, dev_tables, eval_config)]

        instances.append({
            "id": q_id,
            "question": q["question"],
            "db": q.get("db", dataset),
            "tables": tables,
            "prompt": prompt,
            "hints": {
                "mapping": get_mapping(q['column_mapping']) if eval_config.mapping else None,
                "join_keys": get_join_keys(q['join_keys']) if eval_config.join_keys else None,
                "domain_knowledge": get_knowledge(q['domain_knowledge']) if q_knowledge else None,
                "decomposition": get_decomp(q['sub_questions']) if q_decomp else None,
            },
            "record": q,
        })

    if skipped:
        print(f"Skipped {skipped} question(s) with no retrieved tables.")
    print(f"#instances: {len(instances)}")
    return instances
