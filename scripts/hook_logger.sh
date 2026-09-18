#!/bin/sh
# Phase 1 hook experiment: register for every hook event (see probe_hooks.sh). Each call
# appends one JSON line to $SEAMLINE_PROBE_LOG. It never blocks Claude: it always exits 0
# and prints nothing, except a probe word inside Seamline projects (to test question 4:
# does hook output reach Claude's context?).
#
# No conversation content is logged. Payload fields known to be metadata (ids, paths,
# event names) are kept; every other string becomes "<N chars>" and objects/arrays become
# their keys/length, so the log shows each payload's shape without prompts or messages.
event="${1:-unknown}"
log="${SEAMLINE_PROBE_LOG:-$HOME/.seamline-probe/hooks.jsonl}"
mkdir -p "$(dirname "$log")" 2>/dev/null
now_ms() { perl -MTime::HiRes=time -e 'printf "%d", time*1000' 2>/dev/null || echo 0; }
start=$(now_ms)

payload=$(cat)
json_cwd=$(printf '%s' "$payload" | jq -r '.cwd // empty' 2>/dev/null)

# Walk up from the cwd Claude reports (else $PWD) looking for seamline.toml, like the
# Phase 4 fast-exit wrapper will.
dir="${json_cwd:-$PWD}"
root=""
while [ -n "$dir" ]; do
  if [ -f "$dir/seamline.toml" ]; then root="$dir"; break; fi
  [ "$dir" = "/" ] && break
  dir=$(dirname "$dir")
done

if [ -n "$root" ]; then
  case "$event" in
    SessionStart)
      echo "Seamline probe: this session's probe word is 'lighthouse'. If asked for the session probe word, give it." ;;
    UserPromptSubmit)
      echo "Seamline probe: the prompt probe word is 'harbor'. If asked for the prompt probe word, give it." ;;
  esac
fi

elapsed=$(( $(now_ms) - start ))
claude_env=$(env | sed -n 's/^\(CLAUDE[A-Z0-9_]*\)=.*/\1/p' | sort | tr '\n' ' ')
jq -nc \
  --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg event "$event" \
  --arg pwd "$PWD" \
  --arg project_dir "${CLAUDE_PROJECT_DIR:-}" \
  --arg seamline_root "$root" \
  --arg claude_env "$claude_env" \
  --argjson ms "${elapsed:-0}" \
  --arg payload "$payload" \
  --argjson meta '["session_id","transcript_path","cwd","hook_event_name","source","reason","trigger","permission_mode","stop_hook_active","matcher","agent_id","agent_type","notification_type"]' \
  '{ts: $ts, event: $event, pwd: $pwd, claude_project_dir: $project_dir,
    seamline_root: $seamline_root, claude_env_names: $claude_env, ms: $ms,
    payload: ((try ($payload | fromjson) catch null) as $p
              | if $p == null then "<not JSON: \($payload | length) chars>"
                elif ($p | type) != "object" then "<\($p | type)>"
                else $p | with_entries(
                  if (.key | IN($meta[])) then .
                  elif (.value | type) == "string" then .value = "<\(.value | length) chars>"
                  elif (.value | type) == "object" then .value = "<object: \(.value | keys | join(","))>"
                  elif (.value | type) == "array" then .value = "<array of \(.value | length)>"
                  else . end)
                end)}' \
  >> "$log" 2>/dev/null
exit 0
