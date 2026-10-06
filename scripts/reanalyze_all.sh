#!/bin/bash
# Re-run ingestion + analysis for every registered repository (sequentially).
# Usage: scripts/reanalyze_all.sh [http://127.0.0.1:8000]
set -u
API="${1:-http://127.0.0.1:8000}"

ids=$(curl -s "$API/api/repos" | python3 -c 'import sys,json;print(" ".join(str(r["id"]) for r in json.load(sys.stdin)))')
for id in $ids; do
  echo "[reanalyze] repo $id"
  curl -s -X POST "$API/api/repos/$id/reanalyze"; echo
  for i in $(seq 1 1200); do
    row=$(curl -s "$API/api/repos/$id")
    echo "[reanalyze] repo $id: $(echo "$row" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["status"], round((d.get("progress") or 0)*100,1), d.get("message") or "")')"
    status=$(echo "$row" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
    [ "$status" = "ready" ] && break
    [ "$status" = "error" ] && echo "[reanalyze] ERROR: $(echo "$row" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("error"))')" && break
    sleep 3
  done
done
echo "[reanalyze] done"
curl -s "$API/api/repos" | python3 -c 'import sys,json;[print(r["id"],r["name"],r["status"],r["commit_count"]) for r in json.load(sys.stdin)]'
