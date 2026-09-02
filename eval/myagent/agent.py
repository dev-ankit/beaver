"""
============================================================================
 Codex-backed agent for BEAVER text-to-SQL.
 Three generation modes (pick via env), all gold-blind:
   * plain          : one `codex exec` call -> SQL                (default)
   * self-fix       : CODEX_SQL_FIX=1     -> run own SQL, fix execution errors
   * explore+verify : CODEX_SQL_EXPLORE=1 -> run read-only queries against the
                      real tables, see the rows returned, self-check, finalize
============================================================================

The backend-agnostic machinery (response parsing, the read-only SQL guard,
mediated DB access, credential stripping, the explore protocol) lives in
eval/agent_common.py and is shared with eval/claudeagent. This file holds only
the Codex CLI invocation and the Codex-specific mode orchestration.

NOTE on isolation: `codex exec --sandbox read-only` restricts writes and
network but NOT filesystem reads, so a determined model could still read files
by absolute path. This is defense-in-depth for a cooperative model, not a hard
security boundary — true isolation would require running the CLI in a
container without the data/ tree mounted.

  instance fields (built in prompt.py::build_instances):
    id, question, db, tables, prompt (chat messages), hints, record
    -> NOTE: do not read record['sql']; that is the gold answer.

Config (env vars, all optional):
    CODEX_MODEL              Codex model (`codex -m`); unset -> account default.
    CODEX_REASONING_EFFORT   minimal|low|medium|high|xhigh  (default: low)
    CODEX_SANDBOX            read-only|workspace-write|danger-full-access (default: read-only)
    CODEX_TIMEOUT            per codex-call seconds (default: 300)
    CODEX_MAX_RETRIES        retries for a failed/timed-out codex call (default: 2)
    CODEX_BIN               path to codex (default: codex)

    CODEX_SQL_FIX           1 -> fix execution errors via error feedback (default 0)
    CODEX_FIX_ATTEMPTS      max fix rounds (default 2)

    CODEX_SQL_EXPLORE       1 -> explore/verify loop (overrides SQL_FIX) (default 0)
    CODEX_EXPLORE_STEPS     max exploratory query rounds (default 4)
    CODEX_EXPLORE_ROWS      max rows returned per exploratory query (default 20)
    CODEX_FIX_TIMEOUT_MS    SELECT execution cap, ms (default 10000; keep <= the
                            scorer's QUERY_TIMEOUT so a query the agent verifies
                            as OK also passes scoring)
    MYSQL_HOST/USER/PASSWORD  DB creds (env or nearest .env)

    CODEX_DECOMPOSE         1 -> self-decompose the question and validate each
                            sub-step with SQL (requires CODEX_SQL_EXPLORE)
    CODEX_REVIEW            1 -> adversarial review subagent: probes the executor's
                            candidate(s) with its OWN read-only queries (gold-blind)
                            and replaces a candidate only on concrete contradicting
                            evidence; keeps candidates byte-for-byte otherwise
    CODEX_REVIEW_STEPS      max reviewer probe rounds (default 4)
    CODEX_STYLE_GUIDE       1 -> prepend the gold-blind reference-query house style
    CODEX_SCHEMA_SKILL      1 -> inject profiled data facts (dw_schema_skill.json)
                            for the tables in play; profiled read-only from the
                            DB, gold-blind (see scratchpad profile_dw.py)
    CODEX_N_CANDIDATES      N>1 -> emit N candidate queries spanning plausible
                            readings of an ambiguous question, joined by the
                            '-- ===CANDIDATE=== --' marker in one .sql file;
                            score with evaluate_ex_acc.py --multi
"""
import os
import sys
import time
import shutil
import tempfile
import subprocess

# Shared, backend-agnostic primitives (eval/agent_common.py). eval/ is this
# file's grandparent; add it to the path so the import resolves regardless of
# the working directory execute.py is launched from.
_EVAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _EVAL_DIR not in sys.path:
    sys.path.append(_EVAL_DIR)

from agent_common import (  # noqa: E402
    clean_sql,
    render_prompt,
    cli_env as _cli_env,
    is_read_only as _is_read_only,
    timed as _timed,
    DBUnavailable as _DBUnavailable,
    execute_sql as _execute_sql_raw,
    query_preview as _query_preview_raw,
    fix_prompt as _fix_prompt,
    EXPLORE_PROTOCOL as _EXPLORE_PROTOCOL,
    RUN_SQL_RE as _RUN_SQL_RE,
)

# shutil.which resolves the platform's actual executable (codex.cmd on
# Windows, codex elsewhere); bare names in subprocess skip that resolution.
CODEX_BIN = os.getenv("CODEX_BIN") or shutil.which("codex") or "codex"
CODEX_MODEL = os.getenv("CODEX_MODEL")
CODEX_REASONING_EFFORT = os.getenv("CODEX_REASONING_EFFORT", "low")
CODEX_SANDBOX = os.getenv("CODEX_SANDBOX", "read-only")
CODEX_TIMEOUT = int(os.getenv("CODEX_TIMEOUT", "300"))
CODEX_MAX_RETRIES = int(os.getenv("CODEX_MAX_RETRIES", "2"))

CODEX_SQL_FIX = os.getenv("CODEX_SQL_FIX", "0") not in ("0", "", "false", "False")
CODEX_FIX_ATTEMPTS = int(os.getenv("CODEX_FIX_ATTEMPTS", "2"))

CODEX_SQL_EXPLORE = os.getenv("CODEX_SQL_EXPLORE", "0") not in ("0", "", "false", "False")
CODEX_EXPLORE_STEPS = int(os.getenv("CODEX_EXPLORE_STEPS", "4"))
CODEX_EXPLORE_ROWS = int(os.getenv("CODEX_EXPLORE_ROWS", "20"))
CODEX_FIX_TIMEOUT_MS = int(os.getenv("CODEX_FIX_TIMEOUT_MS", "10000"))

# Self-decompose the question (and validate each sub-step with SQL) instead of
# relying on a provided decomposition hint.
CODEX_DECOMPOSE = os.getenv("CODEX_DECOMPOSE", "0") not in ("0", "", "false", "False")
# Final subagent pass: adversarial review of the executor's candidates. The
# reviewer probes with its OWN read-only queries (gold-blind) and replaces a
# candidate only on concrete contradicting evidence.
CODEX_REVIEW = os.getenv("CODEX_REVIEW", "0") not in ("0", "", "false", "False")
CODEX_REVIEW_STEPS = int(os.getenv("CODEX_REVIEW_STEPS", "4"))
# Gold-blind "house style" prior: conventions of this benchmark's reference SQL
# (derived from failure-mode analysis, not from any per-question gold answer).
CODEX_STYLE_GUIDE = os.getenv("CODEX_STYLE_GUIDE", "0") not in ("0", "", "false", "False")
# Schema skill: per-table data facts profiled read-only from the DB (gold-blind).
CODEX_SCHEMA_SKILL = os.getenv("CODEX_SCHEMA_SKILL", "0") not in ("0", "", "false", "False")
# Emit N candidate queries spanning the plausible readings of an ambiguous
# question (1 = classic single answer). Candidates are stored in one .sql file
# joined by _CANDIDATE_SEP; score with evaluate_ex_acc.py --multi.
CODEX_N_CANDIDATES = max(1, int(os.getenv("CODEX_N_CANDIDATES", "1")))
_CANDIDATE_SEP = "\n-- ===CANDIDATE=== --\n"
_SCHEMA_SKILL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dw_schema_skill.json")
_schema_skill_cache = None


def _schema_skill_section(tables):
    """Build the schema-facts block for the tables in play (plus global notes)."""
    global _schema_skill_cache
    if _schema_skill_cache is None:
        import json
        with open(_SCHEMA_SKILL_PATH, encoding="utf-8") as f:
            _schema_skill_cache = json.load(f)
    parts = [_schema_skill_cache["_global"]]
    parts += [_schema_skill_cache[t] for t in tables if t in _schema_skill_cache]
    return (
        "\n\n### Database facts (profiled read-only from the live dw database)\n"
        + "\n\n".join(parts)
    )


# ----------------------- Codex CLI invocation -----------------------

def _codex_call(prompt: str) -> str:
    """One headless `codex exec` call -> raw final message text. Retries a
    failed/timed-out call up to CODEX_MAX_RETRIES times so a transient CLI
    failure does not silently become an empty prediction."""
    last_err = None
    for attempt in range(CODEX_MAX_RETRIES + 1):
        workdir = tempfile.mkdtemp(prefix="codex_beaver_")
        last_msg = os.path.join(workdir, "_last_message.txt")
        cmd = [
            CODEX_BIN, "exec", "--sandbox", CODEX_SANDBOX, "--skip-git-repo-check",
            "-C", workdir, "-c", f"model_reasoning_effort={CODEX_REASONING_EFFORT}", "-o", last_msg,
        ]
        if CODEX_MODEL:
            cmd += ["-m", CODEX_MODEL]
        cmd.append("-")
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                timeout=CODEX_TIMEOUT, env=_cli_env(),
            )
            raw = ""
            if os.path.exists(last_msg):
                with open(last_msg, encoding="utf-8") as f:
                    raw = f.read()
            if raw.strip():
                return raw
            if proc.returncode == 0:
                return raw  # rc==0 with no message: genuine empty answer, do not retry
            tail = ((proc.stdout or "") + (proc.stderr or ""))[-800:].strip()
            last_err = f"codex exec failed (rc={proc.returncode}): {tail}"
        except subprocess.TimeoutExpired:
            last_err = f"codex exec timed out after {CODEX_TIMEOUT}s"
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if attempt < CODEX_MAX_RETRIES:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(last_err or "codex exec failed")


def _codex_generate(prompt: str) -> str:
    return clean_sql(_codex_call(prompt))


# DB access with this backend's execution timeout bound in (the primitives take
# the cap as a parameter; these thin wrappers keep the internal call sites tidy).
def _execute_sql(sql: str, db: str):
    return _execute_sql_raw(sql, db, CODEX_FIX_TIMEOUT_MS)


def _query_preview(sql: str, db: str, max_rows: int):
    return _query_preview_raw(sql, db, max_rows, CODEX_FIX_TIMEOUT_MS)


# ------------------------------- modes -------------------------------

def _fix_loop(base, db, sql):
    """Given a candidate SQL, repair execution errors (error feedback only)."""
    if not sql:
        return sql
    for _ in range(CODEX_FIX_ATTEMPTS):
        try:
            ok, err = _execute_sql(sql, db)
        except _DBUnavailable as e:
            # Can't verify (DB down / bad creds): do NOT rewrite a possibly-correct query.
            print(f"DB unavailable; skipping fix loop, keeping candidate SQL: {e}")
            break
        if ok:
            break
        fixed = _codex_generate(_fix_prompt(base, db, sql, err))
        if not fixed or fixed == sql:
            break
        sql = fixed
    return sql


_STYLE_GUIDE = """\

### Reference-query house style (follow unless the question explicitly says otherwise)
Your answer is scored by exact result-set match against a reference query written in a
rigid data-warehouse house style. Mirror these conventions even when an alternative
reading seems cleaner or more "correct":

1. The final user question is the sole authority on the output. Decomposition
   sub-questions are scaffolding for HOW to structure the query: if a sub-question
   mentions a filter, top-k limit, ranking column, or extra total row that the final
   question does not ask for, do NOT let it into the result. Never add LIMIT unless the
   final question asks for a top/bottom subset. Never emit grand-total or subtotal rows
   unless the final question asks for them.
2. Output exactly the columns the final question asks for, in the order it lists them —
   no extra identifier, ranking, or ordering columns.
3. Aggregate over the raw join result. Do NOT add DISTINCT inside COUNT/SUM/AVG to
   compensate for row duplication introduced by joins — duplicated rows are intended
   weighting in this warehouse. Use COUNT(DISTINCT ...) only when the question
   explicitly says "unique", "distinct", or "different".
4. Implement "X with at least one related Y" by JOINing Y directly (keeping the
   resulting row multiplication in downstream aggregates), not via EXISTS/IN semi-joins.
5. When attaching a secondary block (stats, top-k membership, comparison values) to a
   main block, use LEFT JOIN and keep non-matching rows with NULLs; do not INNER JOIN
   away rows unless the question says to exclude them.
6. Keep raw values raw: no ROUND, no CAST, no STR_TO_DATE/DATE_FORMAT on string-typed
   date columns (compare and MIN/MAX them as plain strings), no reformatting.
7. Window functions: use the simplest form — default frames (no explicit ROWS BETWEEN),
   and ORDER BY inside OVER() only when the question asks for a running/cumulative value.
8. "more than" / "greater than" / "over" = strict >; "at least" / "no less than" = >=
   (and symmetrically for "less than" vs "at most").
9. Use EVERY provided table and EVERY provided join key. If a hinted table looks
   redundant (contributes no output columns, or a more direct join key exists), join it
   anyway along the hinted path and keep the row multiplication it causes — bridge
   tables are part of the intended semantics, not noise to optimize away.\
"""


_DECOMPOSE_GUIDANCE = """\

### Approach: decompose, then validate each part with SQL
Break the question into its sub-steps (filters, joins, groupings, aggregations,
top-k/window pieces). For EACH sub-step, write a small SQL query and RUN it to
confirm that piece returns sensible data — spot-check intermediate results (row
counts, sample values, distinct keys, ranges) so each part looks valid before you
compose them. Only after the parts check out, compose the full query, run it, and
confirm the final rows match the question's intent.\
"""


_MULTI_ANSWER_INSTR = """\

### Final answer format: {n} candidate queries (this replaces the single <ans> format)
Enterprise questions like these often admit more than one defensible reading — the
question text may not pin down choices such as: COUNT vs COUNT(DISTINCT); INNER vs
LEFT JOIN when combining blocks (drop vs keep unmatched rows as NULLs); which of two
similar columns to use (e.g. TERM_CODE vs EFFECTIVE_TERM_CODE); which code value a
phrase maps to; a plain aggregate vs a running/windowed one; whether an auxiliary
filter also applies to a comparison population.

Give your final answer as {n} COMPLETE, independently executable MySQL queries that
span the most plausible readings, each wrapped in its own numbered tag from <ans1> to
<ans{n}> (and matching </ans1> ... </ans{n}>):
<ans1>your best-guess query</ans1>
<ans2>second query, changing the choice you are LEAST sure about</ans2>
... continue through <ans{n}>, each changing a different uncertain choice.

Rules: candidate 1 is your best guess. Candidates must differ SEMANTICALLY (different
rows/values), not by formatting or aliases. Each must return the same columns in the
same order. Do not change choices the question clearly determines.\
"""


def _split_candidates(text: str):
    """Extract <ans1>..</ans1> ... blocks; fall back to a single <ans> block."""
    out = []
    for i in range(1, CODEX_N_CANDIDATES + 1):
        tag, close = f"<ans{i}>", f"</ans{i}>"
        if tag in text and close in text:
            out.append(text.split(tag, 1)[1].split(close, 1)[0].strip())
    if not out:
        single = clean_sql(text)
        if single:
            out = [single]
    return out


def _parse_action(text: str):
    """Return ('answer', sql) or ('run', query) from a model turn.

    A RUN_SQL request takes precedence over a bare '<ans>' *mention* (e.g. the
    model restating the protocol); only a complete <ans>...</ans> span is
    treated as a final answer that overrides an accompanying RUN_SQL."""
    if not text:
        return "answer", ""
    m = _RUN_SQL_RE.search(text)
    has_complete_ans = ("<ans>" in text and "</ans>" in text) or (
        "<ans1>" in text and "</ans1>" in text)
    if m and not has_complete_ans:
        q = text[m.end():].split("<ans", 1)[0]
        q = clean_sql(q) if "```" in q else q.strip()
        return "run", q.strip()
    if CODEX_N_CANDIDATES > 1:
        return "answer", _CANDIDATE_SEP.join(_split_candidates(text))
    return "answer", clean_sql(text)


def _run_explore(base, db):
    guidance = _DECOMPOSE_GUIDANCE if CODEX_DECOMPOSE else ""
    multi = _MULTI_ANSWER_INSTR.format(n=CODEX_N_CANDIDATES) if CODEX_N_CANDIDATES > 1 else ""
    transcript = base + guidance + _EXPLORE_PROTOCOL.format(db=db, steps=CODEX_EXPLORE_STEPS) + multi
    queries_run = 0
    nudged = False
    for step in range(CODEX_EXPLORE_STEPS):
        resp = _codex_call(transcript)
        action, payload = _parse_action(resp)
        if action == "answer" and payload:
            # Enforce the protocol's "verify first" rule with a single nudge.
            if queries_run == 0 and not nudged and step < CODEX_EXPLORE_STEPS - 1:
                nudged = True
                transcript += (
                    f"\n\n### Proposed answer (NOT yet verified)\n{payload}\n\n"
                    "You have not run any verification query. As required, run at least one "
                    "read-only RUN_SQL query to check this answer before giving <ans>."
                )
                continue
            return payload
        if action == "run" and payload:
            queries_run += 1
            result = _query_preview(payload, db, CODEX_EXPLORE_ROWS)
            transcript += (
                f"\n\n### Your query (step {step + 1})\n{payload}\n\n### Result\n{result}\n\n"
                f"Run another RUN_SQL query, or output your final <ans>...</ans>."
            )
        else:
            break
    # Out of steps (or no parseable action): force a final answer. Do NOT fall
    # back to the last exploratory probe — a DESCRIBE/COUNT probe is not an answer.
    if CODEX_N_CANDIDATES > 1:
        final = _codex_call(
            transcript + "\n\nYou must now output ONLY your final answer in the "
            f"<ans1>..</ans1> ... <ans{CODEX_N_CANDIDATES}>..</ans{CODEX_N_CANDIDATES}> format.",
        )
        return _CANDIDATE_SEP.join(_split_candidates(final))
    final = _codex_call(
        transcript + "\n\nYou must now output ONLY your final answer as <ans>YOUR MYSQL QUERY</ans>.",
    )
    return clean_sql(final)


_ADVERSARIAL_REVIEW_PROTOCOL = """\

### Your task: ADVERSARIAL review of the executor's candidate queries
Another agent (the executor) answered the question above with the {n} candidate
quer{ies} shown below, together with a sample of the rows each returns. Your job is
to try to REFUTE each candidate by running your OWN read-only queries — probes that
are DIFFERENT from the candidate itself, chosen to expose an error if one exists:
- recompute one group's aggregate directly from the base tables and compare it to
  the candidate's value for that group;
- check that every literal the candidate filters on actually exists in that column
  (SELECT DISTINCT / COUNT of the value);
- count rows before and after each join to detect unintended fan-out or dropped rows;
- check the output column count and order against what the question asks for;
- check for elements leaked from the decomposition hints that the final question
  does not ask for (LIMIT/top-k, extra filters, grand-total rows, ranking columns).

Merely re-running a candidate is NOT verification.

Verdict rules — be adversarial about EVIDENCE, conservative about EDITS:
- You may REPLACE a candidate only when a probe produced CONCRETE CONTRADICTING
  EVIDENCE: a recomputed number that disagrees, a filter literal that does not exist
  in the data, a join provably dropping/multiplying rows against the question's
  meaning, a column set that does not match the question.
- Style preferences, alternative readings of ambiguous phrasing, or "I would have
  written it differently" are NOT evidence — in that case KEEP the candidate
  byte-for-byte unchanged.
- The candidates deliberately span DIFFERENT plausible readings of the question's
  ambiguities. Do NOT collapse them into one reading; refute a candidate only on
  its own terms.

Respond with EXACTLY ONE of the following each turn (nothing else):

1) To run a read-only probe (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN), output:
RUN_SQL:
<one SQL statement>

2) When done, output the final candidate set — confirmed candidates copied
byte-for-byte, refuted ones replaced by your corrected query:
{tags}

You have at most {steps} probes. Run at least one probe per candidate before finalizing.\
"""


def _split_candidates_indexed(text: str, n: int):
    """Extract <ansK> blocks by index; None where a tag is absent."""
    out = []
    for i in range(1, n + 1):
        tag, close = f"<ans{i}>", f"</ans{i}>"
        if tag in text and close in text:
            out.append(text.split(tag, 1)[1].split(close, 1)[0].strip() or None)
        else:
            out.append(None)
    return out


def _review(question, sql, db):
    """Adversarial review subagent (gold-blind): probes the executor's candidates
    with its own read-only queries and replaces a candidate only on concrete
    contradicting evidence. Works on the whole candidate bundle at once so probes
    are shared and the deliberate spread across readings is preserved."""
    candidates = [c.strip() for c in sql.split(_CANDIDATE_SEP) if c.strip()]
    if not candidates:
        return sql
    n = len(candidates)
    ies = "y" if n == 1 else "ies"
    tags = "\n".join(f"<ans{i}>candidate {i}, confirmed or corrected</ans{i}>" for i in range(1, n + 1))

    shown = []
    for i, cand in enumerate(candidates, 1):
        preview = _query_preview(cand, db, CODEX_EXPLORE_ROWS)
        shown.append(f"#### Candidate {i}\n{cand}\n\n#### Candidate {i} result (sample)\n{preview}")
    transcript = (
        f"A text-to-SQL executor was given this task:\n\nQUESTION:\n{question}\n\n"
        + "\n\n".join(shown)
        + _ADVERSARIAL_REVIEW_PROTOCOL.format(n=n, ies=ies, tags=tags, steps=CODEX_REVIEW_STEPS)
    )
    for step in range(CODEX_REVIEW_STEPS):
        resp = _codex_call(transcript)
        m = _RUN_SQL_RE.search(resp)
        has_final = "<ans1>" in resp and "</ans1>" in resp
        if m and not has_final:
            probe = resp[m.end():].split("<ans", 1)[0]
            probe = clean_sql(probe) if "```" in probe else probe.strip()
            result = _query_preview(probe, db, CODEX_EXPLORE_ROWS)
            transcript += (
                f"\n\n### Your probe (step {step + 1})\n{probe}\n\n### Result\n{result}\n\n"
                "Run another RUN_SQL probe, or output the final candidate set."
            )
            continue
        if has_final:
            reviewed = _split_candidates_indexed(resp, n)
            merged = [r if r else orig for r, orig in zip(reviewed, candidates)]
            return _CANDIDATE_SEP.join(merged)
        break
    # No usable final verdict within budget: keep the executor's candidates.
    return sql


def run_agent(instance: dict, model: str = None) -> str:
    # `model` is a run label only (used for the output-dir name); the backend
    # model is selected by the CODEX_MODEL env var, not this argument.
    base = render_prompt(instance)
    if CODEX_STYLE_GUIDE:
        base += _STYLE_GUIDE
    if CODEX_SCHEMA_SKILL:
        base += _schema_skill_section(instance.get("tables") or [])
    db = instance.get("db") or "dw"
    question = instance.get("question", "")
    if CODEX_DECOMPOSE and not CODEX_SQL_EXPLORE:
        print("warning: CODEX_DECOMPOSE requires CODEX_SQL_EXPLORE=1; ignoring it this run.")
    if CODEX_SQL_EXPLORE:
        sql = _run_explore(base, db)
    elif CODEX_N_CANDIDATES > 1:
        raw = _codex_call(base + _MULTI_ANSWER_INSTR.format(n=CODEX_N_CANDIDATES))
        sql = _CANDIDATE_SEP.join(_split_candidates(raw))
    else:
        sql = _codex_generate(base)
    if CODEX_REVIEW and sql:  # adversarial review (handles single or multi bundle)
        sql = _review(question, sql, db)
    if CODEX_SQL_FIX and sql:  # final error-repair pass (per candidate in multi mode)
        if CODEX_N_CANDIDATES > 1 and _CANDIDATE_SEP in sql:
            fixed = [_fix_loop(base, db, cand.strip()) for cand in sql.split(_CANDIDATE_SEP)]
            sql = _CANDIDATE_SEP.join(c for c in fixed if c)
        else:
            sql = _fix_loop(base, db, sql)
    return sql
