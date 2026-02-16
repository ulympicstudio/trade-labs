#!/bin/bash
# Morning Scan Script
# Automates the pre-market routine

echo "════════════════════════════════════════════════════════════════════════════════"
echo "  TRADE LABS - MORNING SCAN"
echo "  $(date '+%A, %B %d, %Y at %I:%M %p')"
echo "════════════════════════════════════════════════════════════════════════════════"
echo ""

# Navigate to Trade Labs directory
cd /Users/umronalkotob/trade-labs

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
