#!/bin/bash
# Morning Scan Script
# Automates the pre-market routine

echo "════════════════════════════════════════════════════════════════════════════════"
echo "  TRADE LABS - MORNING SCAN"
echo "  $(date '+%A, %B %d, %Y at %I:%M %p')"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""

# Navigate to Trade Labs directory
cd "$HOME/trade-labs"

# Step 0: Ensure latest agent intel is in data/
INTEL_DEST="$HOME/trade-labs/data/agent_intel.json"
INTEL_FOUND=""

# Check if the watcher already delivered it
if [ -f "$INTEL_DEST" ]; then
    age=$(( $(date +%s) - $(stat -f %m "$INTEL_DEST") ))
    if [ "$age" -lt 14400 ]; then   # less than 4 hours old
        sym_count=$(python3 -c "import json; print(len(json.load(open('$INTEL_DEST')).get('symbols',{})))" 2>/dev/null || echo "?")
        echo "✓ agent_intel.json already current (${age}s old, ${sym_count} symbols)"
        INTEL_FOUND=1
    fi
fi

# If not current, poll ~/Downloads for up to 5 minutes
if [ -z "$INTEL_FOUND" ]; then
    echo "⏳ Waiting for agent_intel file in ~/Downloads..."
    echo "   Save the Computer-generated agent_intel_latest.json to ~/Downloads"
    echo "   (the intel_watcher daemon will auto-copy it, or we'll grab it here)"
    echo ""

    deadline=$(( $(date +%s) + 300 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        # Look for any matching file in Downloads
        match=$(find "$HOME/Downloads" -maxdepth 1 -name 'agent_intel*.json' -newer "$0" -print -quit 2>/dev/null || true)
        if [ -n "$match" ]; then
            # Validate
            if python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert 'symbols' in d" "$match" 2>/dev/null; then
                tmp=$(mktemp "${INTEL_DEST}.XXXXXX")
                cp "$match" "$tmp" && mv "$tmp" "$INTEL_DEST"
                sym_count=$(python3 -c "import json; print(len(json.load(open('$INTEL_DEST')).get('symbols',{})))" 2>/dev/null || echo "?")
                echo "✓ Copied $(basename "$match") → data/agent_intel.json  (${sym_count} symbols)"
                INTEL_FOUND=1
                break
            fi
        fi
        # Also check if the watcher delivered it while we waited
        if [ -f "$INTEL_DEST" ]; then
            age=$(( $(date +%s) - $(stat -f %m "$INTEL_DEST") ))
            if [ "$age" -lt 60 ]; then
                echo "✓ intel_watcher delivered agent_intel.json (${age}s ago)"
                INTEL_FOUND=1
                break
            fi
        fi
        sleep 5
    done

    if [ -z "$INTEL_FOUND" ]; then
        echo "⚠️  No agent_intel file found after 5 min — continuing without it"
    fi
fi

echo ""

# Step 1: System Check
echo "STEP 1: Running System Check..."
echo "────────────────────────────────────────────────────────────────────────────────"
python preflight_check.py
check_status=$?

if [ $check_status -ne 0 ]; then
    echo ""
    echo "⚠️  SYSTEM CHECK FAILED"
    echo "Please fix the issues above before scanning."
    exit 1
fi

echo ""
echo ""

# Step 2: Hybrid Scan
echo "STEP 2: Running Hybrid Market Scan..."
echo "────────────────────────────────────────────────────────────────────────────────"
python run_hybrid_trading.py
scan_status=$?

echo ""
echo ""

# Step 3: Summary
echo "════════════════════════════════════════════════════════════════════════════════"
if [ $scan_status -eq 0 ]; then
    echo "  ✅ MORNING SCAN COMPLETE"
else
    echo "  ⚠️  SCAN COMPLETED WITH WARNINGS"
fi
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""
echo "Results saved to: hybrid_scan_*.json"
echo ""
echo "Next Steps:"
echo "  1. Review the approved positions above"
echo "  2. Decide which trades to take"
echo "  3. Place orders in TWS before market open"
echo ""
