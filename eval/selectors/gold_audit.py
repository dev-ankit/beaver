"""Execute every gold query in <run_dir>/gold and report which fail, which
return empty, and which failures look like identifier-case problems (the
platform-dependence finding). Usage: python gold_audit.py <run_dir>"""
import sys
from _common import run_dir
from utils.ex_acc import get_mysql_credentials, execute_sql_with_timeout

base = run_dir(sys.argv[1])
creds = get_mysql_credentials("dw_real")
ok = empty = 0
errs = []
for gf in sorted((base / "gold").glob("*.sql")):
    df, err = execute_sql_with_timeout(gf.read_text(encoding="utf-8").strip(), creds)
    if err is not None:
        errs.append((gf.stem, err))
    elif df is None or df.empty:
        empty += 1
    else:
        ok += 1
print(f"gold ok+nonempty={ok}  empty={empty}  errored={len(errs)}  (of {ok+empty+len(errs)})")
for q, e in errs:
    print(f"  {q}: {e[:110]}")
case = [q for q, e in errs if "doesn't exist" in e or "Unknown column" in e]
print(f"identifier-case suspects (table/alias case; resolve with lower_case_table_names=1): {len(case)} -> {case}")
