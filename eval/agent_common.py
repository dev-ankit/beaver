"""Shared, backend-agnostic primitives for the BEAVER agent harnesses
(eval/myagent = Codex CLI, eval/claudeagent = `claude -p`).

Only the per-backend CLI invocation (`_codex_call` / `_claude_call`) and the
per-backend mode orchestration live in each dir's `agent.py`; everything in
this module is backend- and config-free (timeouts/row-caps are passed in), so
both harnesses share ONE copy of:

  * response parsing        : clean_sql
  * prompt flattening       : render_prompt
  * subprocess hardening    : cli_env  (strips DB creds from the CLI env)
  * read-only SQL guard     : is_read_only (+ helpers)
  * mediated DB access      : connect / execute_sql / query_preview / timed
  * shared prompt fragments : fix_prompt, EXPLORE_PROTOCOL, RUN_SQL_RE

All DB access is mediated by the harness process: the model emits queries
through a text protocol, we execute them read-only and feed back the rows. The
model is never told where the gold files live, and DB credentials are stripped
from the CLI subprocess environment (cli_env), so it cannot reach the database
off the mediated read-only path. This is defense-in-depth for a cooperative
model, not a hard boundary — the sandbox still allows filesystem reads.
"""
import os
import re
import threading

READ_STMTS = ("select", "with", "show", "describe", "desc", "explain")
MAX_FEEDBACK_CHARS = 4000
MAX_CELL_CHARS = 80


_HTML_ENTITY = re.compile(r"&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);")


def _unescape_entities(sql: str) -> str:
    """Undo HTML entity escaping (&lt; &gt; &amp; ...) some models apply inside
    <ans> spans (first seen with gpt-5.6) — it turns comparison operators into
    1064 syntax errors. Only touches strings that actually contain entities."""
    if _HTML_ENTITY.search(sql):
        import html
        return html.unescape(sql)
    return sql


def clean_sql(text: str) -> str:
    """Extract the SQL from a model response.

    Precedence: an <ans>...</ans> span, else the first fenced code block
    (```sql ... ``` or a plain ``` ... ```), else the raw text with a leading
    'SQL:' label removed. A plain fence must not drop the SQL (an earlier
    version returned the prose *before* an un-tagged ``` fence)."""
    if not text:
        return ""
    text = text.strip()
    if "<ans>" not in text and "&lt;ans&gt;" in text:
        text = _unescape_entities(text)
    if "<ans>" in text:
        after = text.split("<ans>", 1)[1]
        return _unescape_entities(after.split("</ans>", 1)[0].strip())
    if "```" in text:
        after = text.split("```", 1)[1]
        # Drop a leading language tag line (```sql / ```mysql / bare ```).
        if "\n" in after:
            first_line, rest = after.split("\n", 1)
            if first_line.strip().lower() in ("sql", "mysql", ""):
                after = rest
        return _unescape_entities(after.split("```", 1)[0].strip())
    if text.lower().startswith("sql:"):
        text = text[len("sql:"):].strip()
    return _unescape_entities(text.strip())


def render_prompt(instance: dict) -> str:
    """Flatten the chat-style prompt into the single string the CLIs expect."""
    blocks = []
    user_seen = 0
    for msg in instance["prompt"]:
        role, content = msg["role"], msg["content"]
        if role == "system":
            blocks.append(content)
        elif role == "assistant":
            blocks.append(f"### Example answer\n{content}")
        elif role == "user":
            user_seen += 1
            header = "### Example input" if user_seen == 1 else "### Now answer this"
            blocks.append(f"{header}\n{content}")
    return "\n\n".join(blocks)


def cli_env():
    """Environment for the model-CLI subprocess: strip DB credentials so the
    model cannot reach the database off the mediated read-only path."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("MYSQL")}


# ----------------------- read-only SQL guard -----------------------

_INTO_FILE = re.compile(r"\binto\s+(outfile|dumpfile)\b", re.IGNORECASE)
_WRITE_IN_CTE = re.compile(r"\b(insert|update|delete|replace)\b", re.IGNORECASE)


def strip_string_literals(sql: str) -> str:
    """Blank out quoted string contents so keyword scans don't match literals."""
    return re.sub(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", "''", sql)


def strip_leading_noise(sql: str) -> str:
    """Drop leading SQL comments and opening parens so the first real keyword can
    be identified — a valid read may start with `-- note`, `/* */`, or `(SELECT`."""
    s = sql.strip()
    while s:
        if s.startswith("--"):
            nl = s.find("\n")
            s = "" if nl == -1 else s[nl + 1:].lstrip()
        elif s.startswith("/*"):
            end = s.find("*/")
            s = "" if end == -1 else s[end + 2:].lstrip()
        elif s.startswith("("):
            s = s[1:].lstrip()
        else:
            break
    return s


def first_keyword(sql: str) -> str:
    s = strip_leading_noise(sql or "")
    return s.split(None, 1)[0].lower() if s else ""


def is_read_only(sql: str) -> bool:
    """True only for genuinely read-only statements. Handles leading
    comments/parens (so valid reads aren't rejected) and blocks write-capable
    constructs whose first keyword looks like a read: `SELECT ... INTO
    OUTFILE/DUMPFILE` and data-modifying CTEs (`WITH ... UPDATE/DELETE`)."""
    if not sql or not sql.strip():
        return False
    first = first_keyword(sql)
    if first not in READ_STMTS:
        return False
    scrubbed = strip_string_literals(sql)
    if _INTO_FILE.search(scrubbed):
        return False
    if first == "with" and _WRITE_IN_CTE.search(scrubbed):
        return False
    return True


# ----------------------- mediated DB access (gold-blind) -----------------------

class DBUnavailable(RuntimeError):
    """The database could not be reached (bad creds / server down) — distinct
    from a SQL error in the model's query, so callers must not treat it as one."""


def load_db_creds():
    if not os.getenv("MYSQL_HOST"):
        try:
            from dotenv import load_dotenv, find_dotenv
            load_dotenv(find_dotenv(usecwd=True))
        except Exception:
            pass
    return os.getenv("MYSQL_HOST", "localhost"), os.getenv("MYSQL_USER", "root"), os.getenv("MYSQL_PASSWORD", "")


def connect(db):
    import mysql.connector
    h, u, p = load_db_creds()
    try:
        return mysql.connector.connect(host=h, user=u, password=p, database=db, connection_timeout=10)
    except Exception as e:
        raise DBUnavailable(str(e))


def timed(fn, timeout_s):
    """Run fn() in a daemon thread with a wall-clock cap; raise TimeoutError if
    it doesn't finish. The SET SESSION cap is server-side and silently absent on
    some servers (MariaDB, old MySQL), so this is the actual backstop against a
    runaway query hanging a worker forever."""
    box = {}

    def _run():
        try:
            box["ok"] = fn()
        except Exception as e:  # noqa: BLE001 - re-raised on the caller thread
            box["err"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"query exceeded {timeout_s:.0f}s client-side timeout")
    if "err" in box:
        raise box["err"]
    return box.get("ok")


def execute_sql(sql: str, db: str, timeout_ms: int):
    """Run read-only under a wall-clock cap that mirrors the scorer (execute +
    fetchall). Returns (ok, error_str). Raises DBUnavailable if the DB can't be
    reached. Never inspects rows."""
    if not is_read_only(sql):
        return False, f"refusing to execute non-read statement (starts with {first_keyword(sql)!r})"
    conn = connect(db)
    timeout_s = timeout_ms / 1000.0
    try:
        cur = conn.cursor()
        try:
            cur.execute(f"SET SESSION max_execution_time={timeout_ms}")
        except Exception:
            pass

        def _do():
            cur.execute(sql)
            cur.fetchall()

        timed(_do, timeout_s)
        return True, ""
    except Exception as e:
        return False, str(e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def query_preview(sql: str, db: str, max_rows: int, timeout_ms: int):
    """Run read-only and return a compact text preview of up to max_rows rows.
    Returns feedback string for the model (cols + rows, or the error)."""
    if not is_read_only(sql):
        return "ERROR: only read-only queries (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN) are allowed here."
    try:
        conn = connect(db)
    except DBUnavailable as e:
        return f"ERROR: could not connect to the `{db}` database (not a problem with your SQL): {e}"
    timeout_s = timeout_ms / 1000.0
    try:
        cur = conn.cursor()
        try:
            cur.execute(f"SET SESSION max_execution_time={timeout_ms}")
        except Exception:
            pass

        def _do():
            cur.execute(sql)
            fetched = cur.fetchmany(max_rows + 1)  # +1 to detect real truncation
            cols = [d[0] for d in cur.description] if cur.description else []
            return fetched, cols

        fetched, cols = timed(_do, timeout_s)
        truncated = len(fetched) > max_rows
        rows = fetched[:max_rows]
        extra = f"\n... (truncated at {max_rows} rows)" if truncated else ""

        def fmt(v):
            s = "NULL" if v is None else str(v)
            return s if len(s) <= MAX_CELL_CHARS else s[:MAX_CELL_CHARS] + "…"

        lines = [" | ".join(cols)] + [" | ".join(fmt(v) for v in r) for r in rows]
        body = "\n".join(lines) + extra
        if len(body) > MAX_FEEDBACK_CHARS:
            body = body[:MAX_FEEDBACK_CHARS] + "\n… (output truncated)"
        return f"{len(rows)} row(s) returned:\n{body}"
    except Exception as e:
        return f"ERROR: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ----------------------- shared prompt fragments -----------------------

def fix_prompt(base, db, bad_sql, error):
    return (
        base + f"\n\n### Execution feedback\nThe query below was run against the `{db}` MySQL "
        f"database and FAILED TO EXECUTE. Fix it so it runs without error, keeping the intended "
        f"logic.\n\nFailed SQL:\n{bad_sql}\n\nDatabase error:\n{error}\n\n"
        f"Return only the corrected MySQL query wrapped in <ans></ans>."
    )


EXPLORE_PROTOCOL = """\

### Database access (read-only) — verification is REQUIRED
You have live read-only access to the `{db}` MySQL database. You will be shown the rows YOUR queries return — you will NOT be shown the expected/correct answer.

You MUST run at least one query to CHECK your candidate answer before finalizing: inspect the real tables (sample rows, distinct values, counts, ranges) to confirm your assumptions, then run your candidate query and verify the returned rows make sense for the question (right columns, plausible row count, filters/joins working, not empty when it shouldn't be). Revise if the results look wrong.

Respond with EXACTLY ONE of the following each turn (nothing else):

1) To run a read-only query (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN), output:
RUN_SQL:
<one SQL statement>

2) Only after you have verified, output your final answer as:
<ans>YOUR FINAL MYSQL QUERY</ans>

You have at most {steps} queries. Do not give <ans> until you have run at least one verification query.\
"""


RUN_SQL_RE = re.compile(r"^\s*RUN_SQL:\s*", re.IGNORECASE | re.MULTILINE)
