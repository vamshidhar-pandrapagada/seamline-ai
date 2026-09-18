#!/bin/sh
# Add or remove the Phase 1 hook logger, for ONE project (recommended) or for every project.
#   scripts/probe_hooks.sh install --project DIR   hooks in DIR/.claude/settings.local.json
#   scripts/probe_hooks.sh uninstall --project DIR removes them (and the file, if it's then empty)
#   scripts/probe_hooks.sh install|uninstall        same, but in the user-level ~/.claude/settings.json
#   scripts/probe_hooks.sh report                   summarizes the log
set -eu
logger="$(cd "$(dirname "$0")" && pwd)/hook_logger.sh"
log="${SEAMLINE_PROBE_LOG:-$HOME/.seamline-probe/hooks.jsonl}"
events="SessionStart UserPromptSubmit Stop SubagentStop PreCompact SessionEnd Notification"

action="${1:-}"
project=""
if [ "${2:-}" = "--project" ]; then
  [ -n "${3:-}" ] || { echo "--project needs a folder" >&2; exit 2; }
  project=$(cd "$3" && pwd)
fi
if [ -n "$project" ]; then
  settings="$project/.claude/settings.local.json"
else
  settings="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
fi

case "$action" in
  install)
    mkdir -p "$(dirname "$settings")"
    if [ -f "$settings" ]; then
      backup="$settings.bak.$(date +%Y%m%d%H%M%S)"
      cp "$settings" "$backup"
      echo "Backup:   $backup"
    else
      echo '{}' > "$settings"
    fi
    tmp=$(mktemp)
    jq --arg logger "$logger" --arg events "$events" '
      reduce ($events | split(" "))[] as $e (.;
        .hooks[$e] = ((.hooks[$e] // [])
          + [{hooks: [{type: "command", command: ($logger + " " + $e)}]}]))' \
      "$settings" > "$tmp"
    mv "$tmp" "$settings"
    echo "Added hook logger for: $events"
    echo "Settings: $settings"
    echo "Log:      $log"
    if [ -n "$project" ] && git -C "$project" rev-parse --git-dir >/dev/null 2>&1 \
       && ! git -C "$project" check-ignore -q "$settings"; then
      echo "warning: $settings is not git-ignored; don't commit it" >&2
    fi
    ;;
  uninstall)
    [ -f "$settings" ] || { echo "Nothing to remove: $settings doesn't exist"; exit 0; }
    tmp=$(mktemp)
    jq --arg logger "$logger" '
      if .hooks then
        .hooks |= (with_entries(.value |= (
                     map(.hooks |= map(select((.command // "") | startswith($logger) | not)))
                     | map(select(.hooks | length > 0))))
                   | with_entries(select(.value | length > 0)))
        | if .hooks == {} then del(.hooks) else . end
      else . end' "$settings" > "$tmp"
    mv "$tmp" "$settings"
    if [ -n "$project" ] && [ "$(jq -c . "$settings")" = "{}" ]; then
      rm "$settings"
      rmdir "$(dirname "$settings")" 2>/dev/null || true
      echo "Removed $settings (it only held the logger)"
    else
      echo "Removed hook logger entries from $settings"
    fi
    ;;
  report)
    [ -f "$log" ] || { echo "No log yet at $log"; exit 0; }
    echo "Calls per event:"
    jq -r '.event' "$log" | sort | uniq -c | sort -rn
    echo; echo "Payload fields per event:"
    jq -r '.event + ": " + (if (.payload|type)=="object" then (.payload|keys|join(",")) else (.payload|tostring) end)' "$log" | sort -u
    echo; echo "Sessions seen (id, cwd from payload, \$PWD, CLAUDE_PROJECT_DIR):"
    jq -r '((.payload | objects) // {}) as $p | [($p.session_id // "?")[0:8], ($p.cwd // "?"), .pwd, .claude_project_dir] | @tsv' "$log" | sort -u
    echo; echo "SessionStart sources:"
    jq -r 'select(.event=="SessionStart") | (.payload | objects | .source) // "?"' "$log" | sort | uniq -c
    echo; echo "Slowest calls (ms):"
    jq -r '[.ms, .event] | @tsv' "$log" | sort -rn | head -3
    ;;
  *)
    echo "usage: $0 install|uninstall [--project DIR] | report" >&2; exit 2 ;;
esac
