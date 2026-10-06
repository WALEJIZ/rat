#!/bin/bash
# Seed a running RAT instance with the three sample repositories from the brief
# (cJSON, Redis, git) by POSTing their public URLs to the ingestion API.
#
# Usage:  scripts/seed_demo.sh [http://127.0.0.1:8000]
set -u
API="${1:-http://127.0.0.1:8000}"

add() {
  echo "[seed] adding $1"
  curl -s -X POST "$API/api/repos/url" -H 'Content-Type: application/json' -d "{\"url\":\"$1\"}"
  echo
}

wait_ready() {
  local id="$1" i out status
  for i in $(seq 1 1200); do
    out=$(curl -s "$API/api/repos/$id")
    status=$(echo "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status"))' 2>/dev/null)
    echo "[seed] repo $id status=$status $(echo "$out" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("message") or "", round((d.get("progress") or 0)*100,1))' 2>/dev/null)"
    if [ "$status" = "ready" ]; then return 0; fi
    if [ "$status" = "error" ]; then
      echo "[seed] ERROR: $(echo "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("error"))')"
      return 1
    fi
    sleep 3
  done
  return 1
}

add "https://github.com/DaveGamble/cJSON.git" && wait_ready 1
add "https://github.com/redis/redis.git" && wait_ready 2
add "https://github.com/git/git.git" && wait_ready 3
echo "[seed] done"
curl -s "$API/api/repos" | python3 -c 'import sys,json;[print(r["id"],r["name"],r["status"],r["commit_count"]) for r in json.load(sys.stdin)]'
