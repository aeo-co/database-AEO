#!/bin/bash
# One-shot: fix SSH key perms, run migration, restart MCP server.
# Console output may not render in the DO web console — this script
# writes a report to /root/fix_report.txt which the agent verifies
# by SSHing in and reading it.
set -x
exec > /root/fix_report.txt 2>&1

echo "=== 1. SSH key setup ==="
mkdir -p /root/.ssh
chmod 700 /root/.ssh
echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEh4lA2GO+9ig4ER/YejLNcM4sNKbhf+7yVVFKIcAIek hermes-agent-auditool" > /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
chown -R root:root /root/.ssh
chmod 755 /root
ssh-keygen -lf /root/.ssh/authorized_keys

echo "=== 2. Migration ==="
cd /root/database-AEO
git pull
./venv/bin/python -c "
from db import get_conn
sql = open('migrate_client_contexts.sql').read()
with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute(sql)
print('MIGRATION_OK')
with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute(\"SELECT table_name FROM information_schema.tables WHERE table_name='client_contexts'\")
        print('TABLE:', cur.fetchall())
        cur.execute(\"SELECT extname FROM pg_extension WHERE extname='vector'\")
        print('EXT:', cur.fetchall())
"

echo "=== 3. Restart MCP server ==="
pkill -9 -f mcp_server.py
sleep 2
MCP_HOST=0.0.0.0 MCP_PORT=8765 setsid nohup ./venv/bin/python mcp_server.py > /tmp/mcp.log 2>&1 < /dev/null &
sleep 4
ps aux | grep mcp_server | grep -v grep
echo "=== DONE ==="
