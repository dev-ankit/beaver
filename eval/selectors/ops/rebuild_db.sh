#!/bin/bash
# Rebuild beaver-mysql with lower_case_table_names=1 (case-insensitive table
# names, matching macOS behavior) so the 12 lowercase-referencing gold queries
# execute. MUST be set at first initialization -> full container+volume rebuild.
# Reuses MYSQL_ROOT_PASSWORD from the RUNNING container's env (never printed).
set -e
D="${DOCKER:-docker}"
REPO="${REPO:-$(cd "$(dirname "$0")/../../.." && pwd)}"
HF="${HF:-$REPO/.venv/Scripts/hf.exe}"   # macOS/Linux: $REPO/.venv/bin/hf
D="${DOCKER:-$D}"

echo "=== capturing password from running container into new container (in-shell only) ==="
PSW=$("$D" exec beaver-mysql sh -c 'printf %s "$MYSQL_ROOT_PASSWORD"')
[ -z "$PSW" ] && { echo "FATAL: could not read password from running container"; exit 1; }

echo "=== stopping and removing old container + volume ==="
"$D" rm -f beaver-mysql
"$D" volume rm beaver-mysql-data

echo "=== recreating with lower_case_table_names=1 ==="
"$D" run --name beaver-mysql -e MYSQL_ROOT_PASSWORD="$PSW" -p 3306:3306 \
  -v beaver-mysql-data:/var/lib/mysql --restart unless-stopped -d mysql:8.0 \
  --local-infile=1 --max_allowed_packet=1G --lower-case-table-names=1
unset PSW

echo "=== waiting for init ==="
for i in $(seq 1 60); do
  if "$D" exec beaver-mysql sh -c 'exec mysql -u root -p"$MYSQL_ROOT_PASSWORD" -N -e "SELECT 1"' 2>/dev/null | grep -q 1; then
    echo "mysql up after ~$((i*5))s"; break
  fi
  sleep 5
done

echo "=== downloading dump ==="
mkdir -p $REPO/_scratch
export PATH="/c/Program Files/nodejs:$PATH"
"$HF" download beaverbench/beaver-table beaver_db.zip --repo-type dataset --local-dir $REPO/_scratch
cd $REPO/_scratch
unzip -o beaver_db.zip "beaver_db/dw.sql"

echo "=== loading dw.sql ==="
"$D" exec -i beaver-mysql sh -c 'exec mysql -u root -p"$MYSQL_ROOT_PASSWORD" --max_allowed_packet=1G' < beaver_db/dw.sql

echo "=== verify ==="
"$D" exec beaver-mysql sh -c 'exec mysql -u root -p"$MYSQL_ROOT_PASSWORD" -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"dw\";"' 2>/dev/null
echo "(expect 97)"
"$D" exec beaver-mysql sh -c 'exec mysql -u root -p"$MYSQL_ROOT_PASSWORD" -N -e "SELECT COUNT(*) FROM dw.employee_directory;"' 2>/dev/null \
  && echo "LOWERCASE TABLE NAME RESOLVES, case-insensitivity confirmed" \
  || echo "FATAL: lowercase reference still fails"
echo "REBUILD_DONE"
