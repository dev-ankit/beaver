"""
============================================================================
 Claude Code (`claude -p`) backed agent for BEAVER text-to-SQL.
 Three generation modes (pick via env), all gold-blind:
   * plain          : one `claude -p` call -> SQL                 (default)
   * self-fix       : CLAUDE_SQL_FIX=1     -> run own SQL, fix execution errors
   * explore+verify : CLAUDE_SQL_EXPLORE=1 -> run read-only queries against the
                      real tables, see the rows returned, self-check, finalize
============================================================================

The backend-agnostic machinery (response parsing, the read-only SQL guard,
mediated DB access, credential stripping, the explore protocol) lives in
eval/agent_common.py and is shared with eval/myagent. This file holds only the
`claude -p` invocation and the Claude-specific mode orchestration.

NOTE on isolation: `claude -p` runs with its default (read) tool access and no
filesystem jail, so a determined model could still read files by absolute path.
This is defense-in-depth for a cooperative model, not a hard security boundary —
true isolation would require running the CLI in a container without the data/
tree mounted (and/or restricting its tools).

The prompt is passed to `claude -p` over stdin (BEAVER prompts are ~30 KB).

  instance fields (built in prompt.py::build_instances):
    id, question, db, tables, prompt (chat messages), hints, record
    -> NOTE: do not read record['sql']; that is the gold answer.

Config (env vars, all optional):
    CLAUDE_BIN              path to the claude binary (default: claude)
    CLAUDE_MODEL            value for `--model` (default: unset -> claude default)
    CLAUDE_TIMEOUT          per claude-call seconds (default: 300)
    CLAUDE_MAX_RETRIES      retries for a failed/timed-out claude call (default: 2)

    CLAUDE_SQL_FIX          1 -> fix execution errors via error feedback (default 0)
    CLAUDE_FIX_ATTEMPTS     max fix rounds (default 2)

    CLAUDE_SQL_EXPLORE      1 -> explore/verify loop (overrides SQL_FIX alone) (default 0)
    CLAUDE_EXPLORE_STEPS    max exploratory query rounds (default 4)
    CLAUDE_EXPLORE_ROWS     max rows returned per exploratory query (default 20)
    CLAUDE_FIX_TIMEOUT_MS   SELECT execution cap, ms (default 10000; keep <= the
                            scorer's QUERY_TIMEOUT so a query the agent verifies
                            as OK also passes scoring)
    MYSQL_HOST/USER/PASSWORD  DB creds (env or nearest .env)
"""
import os
import sys
import time
import shutil
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

# shutil.which resolves the platform's actual executable (claude.cmd on
# Windows, claude elsewhere); bare names in subprocess skip that resolution.
CLAUDE_BIN = os.getenv("CLAUDE_BIN") or shutil.which("claude") or "claude"
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL")  # None -> claude's default model; e.g. "opus", "sonnet"
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT")  # None -> default; e.g. "high" (--effort)
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))
CLAUDE_MAX_RETRIES = int(os.getenv("CLAUDE_MAX_RETRIES", "2"))

CLAUDE_SQL_FIX = os.getenv("CLAUDE_SQL_FIX", "0") not in ("0", "", "false", "False")
CLAUDE_FIX_ATTEMPTS = int(os.getenv("CLAUDE_FIX_ATTEMPTS", "2"))

CLAUDE_SQL_EXPLORE = os.getenv("CLAUDE_SQL_EXPLORE", "0") not in ("0", "", "false", "False")
CLAUDE_EXPLORE_STEPS = int(os.getenv("CLAUDE_EXPLORE_STEPS", "4"))
CLAUDE_EXPLORE_ROWS = int(os.getenv("CLAUDE_EXPLORE_ROWS", "20"))
CLAUDE_FIX_TIMEOUT_MS = int(os.getenv("CLAUDE_FIX_TIMEOUT_MS", "10000"))


# ----------------------- claude -p invocation -----------------------

def _claude_call(prompt: str) -> str:
    """One headless `claude -p` call -> raw stdout text. Retries a failed/timed-out
    call up to CLAUDE_MAX_RETRIES times so a transient CLI failure does not
    silently become an empty prediction."""
    cmd = [CLAUDE_BIN, "-p", "--output-format", "text"]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    if CLAUDE_EFFORT:
        cmd += ["--effort", CLAUDE_EFFORT]
    last_err = None
    for attempt in range(CLAUDE_MAX_RETRIES + 1):
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                timeout=CLAUDE_TIMEOUT, env=_cli_env(),
            )
        except subprocess.TimeoutExpired:
            last_err = f"claude -p timed out after {CLAUDE_TIMEOUT}s"
        else:
            out = proc.stdout or ""
            if out.strip() or proc.returncode == 0:
                return out
            last_err = f"claude -p failed (rc={proc.returncode}): {(proc.stderr or '')[-800:].strip()}"
        if attempt < CLAUDE_MAX_RETRIES:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(last_err or "claude -p failed")


def _claude_generate(prompt: str) -> str:
    return clean_sql(_claude_call(prompt))


# DB access with this backend's execution timeout bound in (the primitives take
# the cap as a parameter; these thin wrappers keep the internal call sites tidy).
def _execute_sql(sql: str, db: str):
    return _execute_sql_raw(sql, db, CLAUDE_FIX_TIMEOUT_MS)


def _query_preview(sql: str, db: str, max_rows: int):
    return _query_preview_raw(sql, db, max_rows, CLAUDE_FIX_TIMEOUT_MS)


# ------------------------------- modes -------------------------------

def _fix_loop(base, db, sql):
    """Given a candidate SQL, repair execution errors (error feedback only)."""
    if not sql:
        return sql
    for _ in range(CLAUDE_FIX_ATTEMPTS):
        try:
            ok, err = _execute_sql(sql, db)
        except _DBUnavailable as e:
            # Can't verify (DB down / bad creds): do NOT rewrite a possibly-correct query.
            print(f"DB unavailable; skipping fix loop, keeping candidate SQL: {e}")
            break
        if ok:
            break
        fixed = _claude_generate(_fix_prompt(base, db, sql, err))
        if not fixed or fixed == sql:
            break
        sql = fixed
    return sql


def _parse_action(text: str):
    """Return ('answer', sql) or ('run', query) from a model turn.

    A RUN_SQL request takes precedence over a bare '<ans>' *mention* (e.g. the
    model restating the protocol); only a complete <ans>...</ans> span is
    treated as a final answer that overrides an accompanying RUN_SQL."""
    if not text:
        return "answer", ""
    m = _RUN_SQL_RE.search(text)
    has_complete_ans = "<ans>" in text and "</ans>" in text
    if m and not has_complete_ans:
        q = text[m.end():].split("<ans>", 1)[0]
        q = clean_sql(q) if "```" in q else q.strip()
        return "run", q.strip()
    return "answer", clean_sql(text)


def _run_explore(base, db):
    transcript = base + _EXPLORE_PROTOCOL.format(db=db, steps=CLAUDE_EXPLORE_STEPS)
    queries_run = 0
    nudged = False
    for step in range(CLAUDE_EXPLORE_STEPS):
        resp = _claude_call(transcript)
        action, payload = _parse_action(resp)
        if action == "answer" and payload:
            # Enforce the protocol's "verify first" rule with a single nudge.
            if queries_run == 0 and not nudged and step < CLAUDE_EXPLORE_STEPS - 1:
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
            result = _query_preview(payload, db, CLAUDE_EXPLORE_ROWS)
            transcript += (
                f"\n\n### Your query (step {step + 1})\n{payload}\n\n### Result\n{result}\n\n"
                f"Run another RUN_SQL query, or output your final <ans>...</ans>."
            )
        else:
            break
    # Out of steps (or no parseable action): force a final answer. Do NOT fall
    # back to the last exploratory probe — a DESCRIBE/COUNT probe is not an answer.
    final = _claude_call(
        transcript + "\n\nYou must now output ONLY your final answer as <ans>YOUR MYSQL QUERY</ans>.",
    )
    return clean_sql(final)


def run_agent(instance: dict, model: str = None) -> str:
    # `model` is a run label only (used for the output-dir name); the backend
    # model is selected by the CLAUDE_MODEL env var, not this argument.
    base = render_prompt(instance)
    db = instance.get("db") or "dw"
    if CLAUDE_SQL_EXPLORE:
        sql = _run_explore(base, db)
    else:
        sql = _claude_generate(base)
    if CLAUDE_SQL_FIX and sql:  # final error-repair pass
        sql = _fix_loop(base, db, sql)
    return sql
