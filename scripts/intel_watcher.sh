#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# intel_watcher.sh — persistent daemon that watches ~/Downloads for
# agent_intel*.json files dropped by the pre-market catalyst scanner.
#
# On detection: validates JSON, copies atomically to
# ~/trade-labs/data/agent_intel.json, and logs the event.
#
# Requires: fswatch (brew install fswatch)
# ──────────────────────────────────────────────────────────────────────
set -uo pipefail

WATCH_DIR="$HOME/trade-labs/intel_drop"
DEST="$HOME/trade-labs/data/agent_intel.json"
LOG="$HOME/trade-labs/logs/intel_watcher.log"

mkdir -p "$(dirname "$DEST")" "$(dirname "$LOG")"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

# ── Ensure fswatch is available ──────────────────────────────────────
if ! command -v fswatch &>/dev/null; then
    echo "fswatch not found — installing via Homebrew..."
    if command -v brew &>/dev/null; then
        brew install fswatch
    else
        echo "ERROR: Homebrew not found. Install fswatch manually." >&2
        exit 1
    fi
fi

log "intel_watcher started  watch_dir=$WATCH_DIR  dest=$DEST"

# ── Validate + atomic copy ───────────────────────────────────────────
process_file() {
    local src="$1"

    # Ignore partial downloads (Chrome/Safari temp suffixes)
    case "$src" in *.crdownload|*.download|*.part|*.tmp) return ;; esac

    # Must still exist (fswatch can fire after deletion)
    [[ -f "$src" ]] || return 0

    local base
    base="$(basename "$src")"

    # Validate JSON structure
    if ! python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
assert 'symbols' in d,       'missing symbols key'
assert 'generated_at' in d,  'missing generated_at key'
print(len(d['symbols']))
" "$src" 2>/dev/null; then
        log "REJECT  file=$base  reason=invalid_json_or_missing_keys"
        return 0
    fi

    local sym_count
    sym_count=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))['symbols']))" "$src" 2>/dev/null)

    # Atomic copy: write to temp file in target dir, then mv into place
    local tmp
    tmp="$(mktemp "${DEST}.XXXXXX")"
    if cp "$src" "$tmp" && mv "$tmp" "$DEST"; then
        log "COPIED  file=$base  symbols=$sym_count  dest=$DEST"
    else
        rm -f "$tmp" 2>/dev/null
        log "ERROR   file=$base  reason=copy_failed"
    fi
}

# ── Watch loop (fswatch blocks; never exits unless killed) ───────────
fswatch -0 --event Created --event Updated --event Renamed \
    --include 'agent_intel.*\.json$' --exclude '.*' \
    "$WATCH_DIR" \
| while IFS= read -r -d '' filepath; do
    process_file "$filepath"
done
