#!/usr/bin/env bash
# a2a-bridge shell client — curl + python3(stdlib) for JSON signing.
#
# Env: A2A_RELAY, A2A_SECRET, A2A_AGENT
#
#   a2a.sh send <to> "<text>"       # send a signed message
#   a2a.sh poll                     # long-poll once (relay default 30s)
#   a2a.sh ack <id>                 # ack a delivered message
#   a2a.sh result <reply_to_id> "<text>"  # send a result back (routes via /resolve)
#   a2a.sh health                   # relay health check

set -euo pipefail
: "${A2A_RELAY:=http://127.0.0.1:8765}"
: "${A2A_SECRET:?set A2A_SECRET}"
: "${A2A_AGENT:?set A2A_AGENT}"

_sign() {
  # $1=to  $2=type  $3=content  $4=reply_to(or "")
  TO="$1" MTYPE="$2" CONTENT="$3" REPLY_TO="$4" python3 -c '
import json, os, uuid, hashlib, hmac
from datetime import datetime, timezone
to, mtype, content, reply_to = os.environ["TO"], os.environ["MTYPE"], os.environ["CONTENT"], os.environ["REPLY_TO"]
secret = os.environ["A2A_SECRET"].encode()
try:
    content = json.loads(content)
except Exception:
    pass
m = {
    "id": str(uuid.uuid4()), "from": os.environ["A2A_AGENT"], "to": to,
    "type": mtype, "content": content,
    "reply_to": reply_to if reply_to else None,
    "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
}
canon = (m["id"] + m["from"] + m["to"] + m["type"] + m["timestamp"]
         + json.dumps(m["content"], sort_keys=True, separators=(",", ":")))
m["hmac"] = hmac.new(secret, canon.encode(), hashlib.sha256).hexdigest()
print(json.dumps({"message": m}))
'
}

case "${1:-}" in
  send)
    body=$(_sign "${2:?to}" "message" "${3:-}" "")
    curl -sS -X POST -H "X-A2A-Secret: $A2A_SECRET" -H 'Content-Type: application/json' -d "$body" "$A2A_RELAY/send"; echo ;;
  poll)
    curl -sS -H "X-A2A-Secret: $A2A_SECRET" "$A2A_RELAY/inbox?agent=$A2A_AGENT"; echo ;;
  ack)
    curl -sS -X DELETE -H "X-A2A-Secret: $A2A_SECRET" "$A2A_RELAY/inbox/${2:?id}?agent=$A2A_AGENT"; echo ;;
  result)
    # resolve sender of reply_to, then sign+send a result addressed to them
    sender=$(curl -sS -H "X-A2A-Secret: $A2A_SECRET" "$A2A_RELAY/resolve/${2:?reply_to_id}" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("sender",""))')
    if [ -z "$sender" ]; then echo "could not resolve sender for ${2}" >&2; exit 1; fi
    body=$(_sign "$sender" "result" "${3:-}" "${2}")
    curl -sS -X POST -H "X-A2A-Secret: $A2A_SECRET" -H 'Content-Type: application/json' -d "$body" "$A2A_RELAY/send"; echo ;;
  health)
    curl -sS -H "X-A2A-Secret: $A2A_SECRET" "$A2A_RELAY/health"; echo ;;
  *)
    echo "usage: a2a.sh {send <to> <text> | poll | ack <id> | result <reply_to> <text> | health}"; exit 1 ;;
esac
