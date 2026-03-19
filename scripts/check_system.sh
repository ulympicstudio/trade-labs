#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Trade Labs — System Health Check
#
# Usage:
#   ./scripts/check_system.sh                   # uses default log
#   ./scripts/check_system.sh /tmp/custom.log   # use specific log
#
# Scans the latest dev runner log for key health indicators.
# ─────────────────────────────────────────────────────────────
set -euo pipefail

LOG="${1:-/tmp/tradelabs_daily.log}"

if [[ ! -f "$LOG" ]]; then
  echo "❌ Log not found: $LOG"
  echo "   Start the system first, or specify a log file."
  exit 1
fi

echo "════════════════════════════════════════════════════"
echo "  Trade Labs — System Health Check"
echo "  Log: $LOG  ($(wc -l < "$LOG") lines)"
echo "════════════════════════════════════════════════════"
echo ""

# ── Helper ─────────────────────────────────────────────────
check() {
  local label="$1" pattern="$2"
  local count
  count=$(grep -c "$pattern" "$LOG" 2>/dev/null || true)
  if [[ "$count" -gt 0 ]]; then
    printf "  ✅  %-28s %s occurrences\n" "$label" "$count"
  else
    printf "  ❌  %-28s NOT FOUND\n" "$label"
  fi
}

last_match() {
  local label="$1" pattern="$2"
  local line
  line=$(grep "$pattern" "$LOG" 2>/dev/null | tail -1 || true)
  if [[ -n "$line" ]]; then
    printf "  📌 Last: %s\n" "${line:0:120}"
  fi
}

echo "── Arm Heartbeats ────────────────────────────────"
for arm in ingest signal risk execution monitor; do
  check "$arm heartbeat" "$arm | heartbeat\|$arm.*heartbeat"
done
echo ""

echo "── News Pipeline ─────────────────────────────────"
check "News poll"              "News poll"
check "Benzinga articles"      "benzinga="
check "GNews articles"         "gnews="
echo ""

echo "── Consensus Detection ───────────────────────────"
check "story_fp_consensus"     "story_fp_consensus"
check "consensus_hits"         "consensus_hits"
check "CONSENSUS: tag"         "CONSENSUS_TAG_APPLIED\|news_consensus_rx"
check "fp_provider_sets"       "fp_provider_sets_sample\|fp_consensus_matches"
last_match "consensus" "consensus_hits"
echo ""

echo "── Canonicalization ──────────────────────────────"
check "GNews canonicalization" "GNews canonicalization"
check "gnews_domain_dist"      "gnews_domain_dist"
check "canon_sample"           "canon_sample"
echo ""

echo "── Signal Arm ────────────────────────────────────"
check "RSI evaluation"         "rsi14="
check "TradeIntent emitted"    "TradeIntent emitted"
check "CONSENSUS_SIGNAL"       "CONSENSUS_SIGNAL"
check "News Shock Engine"      "news_shock"
check "EventScore"             "event_score.*es="
check "event_gate_skip"        "event_gate_skip"
check "event_gate_soft"        "event_gate_soft"
check "consensus_bypass_rsi"   "consensus_bypass_rsi"
check "regime_gate_skip"       "regime_gate_skip"
check "spread_gate_skip"       "spread_gate_skip"
check "session_state"          "session_state="
check "obs_gates (armed/fired)" "obs_gates"
echo ""

echo "── Risk Path ─────────────────────────────────────"
check "risk_path"              "risk_path"
echo ""

echo "── Open Plan Gating ──────────────────────────────"
check "open_plan_candidate_created" "open_plan_candidate_created"
check "open_plan_blocked"      "open_plan_blocked"
check "open_plan_approved"     "open_plan_approved"
check "blueprint_source"       "blueprint_source"
echo "  ── Path breakdown:"
opc_intent=$(grep -c "blueprint_source.*source=trade_intent" "$LOG" 2>/dev/null || true)
opc_plan=$(grep -c "blueprint_source.*source=open_plan" "$LOG" 2>/dev/null || true)
printf "    trade_intent path: %s   open_plan path: %s\n" "$opc_intent" "$opc_plan"
echo ""

echo "── Regime / Squeeze ──────────────────────────────"
check "Regime heartbeat"       "regime="
check "risk_regime"            "risk_regime"
check "squeeze_watchlist"      "squeeze_watchlist_added"
check "Squeeze tracking"       "squeeze="
check "Vol regime"             "vol_regime\|risk_vol_regime"
echo ""

echo "── Symbol Validation ─────────────────────────────"
check "symbol_rejected"        "symbol_rejected"
echo ""

echo "── Risk / Execution ──────────────────────────────"
check "Risk heartbeat"         "risk.*heartbeat"
check "risk_eventsize"         "risk_eventsize"
check "OrderBlueprint"         "OrderBlueprint\|blueprint"
check "PAPER_FILL"             "PAPER_FILL"
check "Escalation"             "escalation"
check "order_event subscriber" "order_event"
echo ""

echo "── Execution Order Events ────────────────────────"
check "FILLED events"          "order_event.*type=FILLED\|event_type=FILLED"
check "REJECTED events"        "order_event.*type=REJECTED\|event_type=REJECTED"
check "CANCELLED events"       "order_event.*type=CANCELLED\|event_type=CANCELLED"
check "PAPER_FILL events"      "PAPER_FILL"
bus_drops=$(grep -c "bus_drop topic=tl.execution.order_event" "$LOG" 2>/dev/null || true)
if [[ "$bus_drops" -gt 0 ]]; then
  printf "  ⚠️   order_event bus_drops     %s (should be 0)\n" "$bus_drops"
else
  echo "  ✅  No order_event bus_drops"
fi
echo ""

echo "── Kill Switch ─────────────────────────────────"
ks_block=$(grep -c "killswitch BLOCK\|CIRCUIT_BREAKER.*BLOCK\|killswitch.*BLOCK" "$LOG" 2>/dev/null || true)
ks_reduce=$(grep -c "killswitch REDUCE\|CIRCUIT_BREAKER.*REDUCE\|killswitch.*REDUCE" "$LOG" 2>/dev/null || true)
ks_pass=$(grep -c "killswitch PASS\|PAPER_FILL" "$LOG" 2>/dev/null || true)
printf "  📊 killswitch BLOCK=%s  REDUCE=%s  PASS=%s\n" "$ks_block" "$ks_reduce" "$ks_pass"
check "breakers heartbeat"     "breakers:"
check "ATR spike breaker"      "atr_spike\|ATR_SPIKE"
check "Loss streak pause"      "loss_streak_pause\|LOSS_STREAK"
echo ""

echo "── Sector Intelligence ─────────────────────────"
check "sector_intel scoring"   "sector_name="
check "sector_monitor summary" "sector_monitor summary"
check "sector_state_change"    "sector_state_change"
sl_block=$(grep -c "sector_limit_block" "$LOG" 2>/dev/null || true)
sl_reduce=$(grep -c "sector_limit_reduce" "$LOG" 2>/dev/null || true)
sl_risk=$(grep -c "risk_sector" "$LOG" 2>/dev/null || true)
printf "  📊 sector_limit BLOCK=%s  REDUCE=%s  risk_sector=%s\n" "$sl_block" "$sl_reduce" "$sl_risk"
check "board sector field"     "sector="
nonzero_sector=$(grep "event_score_breakdown" "$LOG" 2>/dev/null | grep -v "sector=0 " | head -1 || true)
if [[ -n "$nonzero_sector" ]]; then
  printf "  ✅  %-28s YES\n" "non-zero sector score"
  printf "  📌 Sample: %s\n" "${nonzero_sector:0:120}"
else
  printf "  ⚠️   %-28s not found (ok if no BULLISH/HOT states)\n" "non-zero sector score"
fi
# Force-path sector checks (informational)
fi_count=$(grep -c "forced_intent_symbol" "$LOG" 2>/dev/null || true)
if [[ "$fi_count" -gt 0 ]]; then
  printf "  📊 forced_intent_symbol      %s occurrences\n" "$fi_count"
  last_match "forced_intent_symbol" "forced_intent_symbol"
fi
fl_count=$(grep -c "FORCE-SECTOR-LIMITS ACTIVE" "$LOG" 2>/dev/null || true)
if [[ "$fl_count" -gt 0 ]]; then
  printf "  📊 FORCE-SECTOR-LIMITS       %s\n" "$fl_count"
  last_match "FORCE-SECTOR-LIMITS" "FORCE-SECTOR-LIMITS ACTIVE"
fi
st_count=$(grep -c "sector_top=" "$LOG" 2>/dev/null || true)
if [[ "$st_count" -gt 0 ]]; then
  printf "  📊 sector_top heartbeats     %s\n" "$st_count"
  last_match "sector_top" "sector_top="
fi
echo ""

echo "── Volatility Leadership Engine ───────────────────"
check "volatility_leader scoring" "volatility_leader sym="
check "volatility_monitor summary" "volatility_monitor summary"
check "VOLATILITY_SIGNAL"          "VOLATILITY_SIGNAL"
check "risk_volatility"            "risk_volatility"
vl_state=$(grep -c "vol_state_change" "$LOG" 2>/dev/null || true)
if [[ "$vl_state" -gt 0 ]]; then
  printf "  📊 vol_state_change          %s\n" "$vl_state"
  last_match "vol_state_change" "vol_state_change"
fi
vl_gate=$(grep -c "volatility_gate_skip" "$LOG" 2>/dev/null || true)
if [[ "$vl_gate" -gt 0 ]]; then
  printf "  📊 volatility_gate_skip      %s\n" "$vl_gate"
fi
pb_count=$(grep -c "playbook_balance" "$LOG" 2>/dev/null || true)
if [[ "$pb_count" -gt 0 ]]; then
  printf "  📊 playbook_balance          %s\n" "$pb_count"
  last_match "playbook_balance" "playbook_balance"
fi
vl_hb=$(grep -c "vol_leaders=" "$LOG" 2>/dev/null || true)
if [[ "$vl_hb" -gt 0 ]]; then
  printf "  📊 vol_leaders heartbeats    %s\n" "$vl_hb"
  last_match "vol_leaders" "vol_leaders="
fi
vol_engine=$(grep -c "VOLATILITY_ENGINE enabled" "$LOG" 2>/dev/null || true)
if [[ "$vol_engine" -gt 0 ]]; then
  printf "  ✅  %-28s %s\n" "VOLATILITY_ENGINE banner" "$vol_engine"
fi
echo ""

echo "── Industry Rotation Engine ─────────────────────"
check "industry_rotation scoring"  "industry_rotation sym="
check "industry_rotation summary"  "industry_rotation summary"
check "risk_rotation"              "risk_rotation"
ir_state=$(grep -c "industry_rotation_state_change" "$LOG" 2>/dev/null || true)
if [[ "$ir_state" -gt 0 ]]; then
  printf "  📊 rotation_state_change     %s\n" "$ir_state"
  last_match "rotation_state_change" "industry_rotation_state_change"
fi
rl_count=$(grep -c "rotation_leaders=" "$LOG" 2>/dev/null || true)
if [[ "$rl_count" -gt 0 ]]; then
  printf "  📊 rotation_leaders          %s\n" "$rl_count"
  last_match "rotation_leaders" "rotation_leaders="
fi
re_count=$(grep -c "rotation_exposure=" "$LOG" 2>/dev/null || true)
if [[ "$re_count" -gt 0 ]]; then
  printf "  📊 rotation_exposure hb      %s\n" "$re_count"
  last_match "rotation_exposure" "rotation_exposure="
fi
rot_engine=$(grep -c "ROTATION_ENGINE enabled" "$LOG" 2>/dev/null || true)
if [[ "$rot_engine" -gt 0 ]]; then
  printf "  ✅  %-28s %s\n" "ROTATION_ENGINE banner" "$rot_engine"
fi
rot_force=$(grep -c "FORCE-INDUSTRY-ROTATION ACTIVE" "$LOG" 2>/dev/null || true)
if [[ "$rot_force" -gt 0 ]]; then
  printf "  📊 FORCE-INDUSTRY-ROTATION   %s\n" "$rot_force"
fi
echo ""

echo "── Dynamic Universe + Sector Rotation (Phase B) ──"
check "sector_rotation_decision"    "sector_rotation_decision"
check "dynamic_universe"            "dynamic_universe"
check "scan_schedule"               "scan_schedule"
check "dynamic_universe_summary"    "dynamic_universe_summary"
check "allocation_rotation_boost"   "allocation_rotation_boost"
check "risk_rotation_bias"          "risk_rotation_bias"
rot_dec=$(grep -c "sector_rotation_decision" "$LOG" 2>/dev/null || true)
if [[ "$rot_dec" -gt 0 ]]; then
  printf "  📊 rotation decisions        %s\n" "$rot_dec"
  last_match "rotation_decision" "sector_rotation_decision"
fi
dyn_count=$(grep -c "dynamic_universe" "$LOG" 2>/dev/null | head -1 || true)
if [[ "$dyn_count" -gt 0 ]]; then
  printf "  📊 dynamic_universe updates  %s\n" "$dyn_count"
  last_match "dynamic_universe" "dynamic_universe"
fi
sched_count=$(grep -c "scan_schedule" "$LOG" 2>/dev/null || true)
if [[ "$sched_count" -gt 0 ]]; then
  printf "  📊 scan_schedule updates     %s\n" "$sched_count"
  last_match "scan_schedule" "scan_schedule"
fi
echo ""

echo "── Allocation / Orchestration Engine ─────────────"
check "allocation_decision"        "allocation_decision regime="
check "symbol_priority"            "symbol_priority sym="
check "confluence_hit"             "confluence_hit sym="
check "risk_allocation"            "risk_allocation symbol="
check "allocation_monitor"         "allocation_monitor summary"
tb_count=$(grep -c "top_confluence" "$LOG" 2>/dev/null || true)
if [[ "$tb_count" -gt 0 ]]; then
  printf "  📊 top_confluence            %s\n" "$tb_count"
fi
bu_count=$(grep -c "bucket_usage" "$LOG" 2>/dev/null || true)
if [[ "$bu_count" -gt 0 ]]; then
  printf "  📊 bucket_usage              %s\n" "$bu_count"
fi
alloc_engine=$(grep -c "ALLOCATION_ENGINE enabled" "$LOG" 2>/dev/null || true)
if [[ "$alloc_engine" -gt 0 ]]; then
  printf "  ✅  %-28s %s\n" "ALLOCATION_ENGINE banner" "$alloc_engine"
fi
alloc_force=$(grep -c "FORCE-ALLOCATION ACTIVE" "$LOG" 2>/dev/null || true)
if [[ "$alloc_force" -gt 0 ]]; then
  printf "  📊 FORCE-ALLOCATION          %s\n" "$alloc_force"
fi
echo ""

echo "── Market Mode / Session Commander ────────────────"
check "market_mode_decision"       "market_mode_decision mode="
check "allocation_mode_adjustment" "allocation_mode_adjustment mode="
check "symbol_mode_fit"            "symbol_mode_fit sym="
check "risk_mode_adjustment"       "risk_mode_adjustment symbol="
check "market_mode summary"        "market_mode summary mode="
check "mode_posture"               "mode_posture mode="
mm_engine=$(grep -c "MARKET_MODE_ENGINE enabled" "$LOG" 2>/dev/null || true)
if [[ "$mm_engine" -gt 0 ]]; then
  printf "  ✅  %-28s %s\n" "MARKET_MODE_ENGINE banner" "$mm_engine"
fi
mm_force=$(grep -c "FORCE-MARKET-MODE ACTIVE" "$LOG" 2>/dev/null || true)
if [[ "$mm_force" -gt 0 ]]; then
  printf "  📊 FORCE-MARKET-MODE         %s\n" "$mm_force"
fi
echo ""

echo "── Playbook Scorecard Engine ───────────────────────"
check "scorecard_open"               "scorecard_open intent="
check "scorecard_close"              "scorecard_close intent="
check "playbook_score_update"        "playbook_score_update bucket="
check "scorecard_weight_adjustment"  "scorecard_weight_adjustment pre="
check "risk_scorecard_adjustment"    "risk_scorecard_adjustment symbol="
check "scorecard_fit"                "scorecard_fit sym="
check "scorecard_monitor summary"    "scorecard_monitor summary"
sc_engine=$(grep -c "SCORECARD_ENGINE enabled" "$LOG" 2>/dev/null || true)
if [[ "$sc_engine" -gt 0 ]]; then
  printf "  ✅  %-28s %s\n" "SCORECARD_ENGINE banner" "$sc_engine"
fi
sc_force=$(grep -c "force_playbook=" "$LOG" 2>/dev/null | head -1 || true)
sc_force=${sc_force:-0}
if [[ "$sc_force" -gt 0 ]]; then
  printf "  📊 FORCE-SCORECARD           %s\n" "$sc_force"
fi
echo ""

echo "── Exit Intelligence Engine ────────────────────────"
check "exit_register"              "exit_register symbol="
check "exit_decision"              "exit_decision symbol="
check "exit_trim"                  "exit_trim symbol="
check "exit_full"                  "exit_full symbol="
check "exit_time_stop"             "exit_time_stop symbol="
check "exit_mode_adjustment"       "exit_mode_adjustment symbol="
check "exit_scorecard_adjustment"  "exit_scorecard_adjustment symbol="
check "exit_monitor summary"       "exit_monitor summary"
check "open_positions"             "open_positions count="
check "exit_watchlist"             "exit_watchlist count="
exit_engine=$(grep -c "EXIT_ENGINE enabled" "$LOG" 2>/dev/null || true)
if [[ "$exit_engine" -gt 0 ]]; then
  printf "  ✅  %-28s %s\n" "EXIT_ENGINE banner" "$exit_engine"
fi
exit_force=$(grep -c "force_playbook=\|force_mode=\|force_action=" "$LOG" 2>/dev/null | head -1 || true)
exit_force=${exit_force:-0}
if [[ "$exit_force" -gt 0 ]]; then
  printf "  📊 EXIT force-path           %s\n" "$exit_force"
fi
echo ""

echo "── PnL Attribution + Self-Tuning ─────────────────"
check "attribution_summary"        "attribution_summary total="
check "attrib_open"                "attrib_open symbol="
check "attrib_close"               "attrib_close symbol="
check "attrib_fill"                "attrib_fill symbol="
check "top_winners"                "top_winners"
check "top_losers"                 "top_losers"
check "attrib_buckets"             "attrib_buckets"
check "attrib_modes"               "attrib_modes"
check "tuning_summary"             "tuning_summary decisions="
check "tuning_decision"            "tuning_decision dim="
check "tuning_override_applied"    "tuning_override_applied"
check "tuned_bucket_weight"        "tuned_bucket_weight"
check "tuned_priority_bias"        "tuned_priority_bias"
check "tuned_threshold"            "tuned_threshold"
check "risk_tuning_adjustment"     "risk_tuning_adjustment"
check "active_overrides"           "active_overrides count="
attrib_eng=$(grep -c "ATTRIBUTION_ENGINE enabled" "$LOG" 2>/dev/null || true)
if [[ "$attrib_eng" -gt 0 ]]; then
  printf "  ✅  %-28s %s\n" "ATTRIBUTION_ENGINE banner" "$attrib_eng"
fi
tuning_eng=$(grep -c "TUNING_ENGINE enabled" "$LOG" 2>/dev/null || true)
if [[ "$tuning_eng" -gt 0 ]]; then
  printf "  ✅  %-28s %s\n" "TUNING_ENGINE banner" "$tuning_eng"
fi
echo ""

echo "── Errors ────────────────────────────────────────"
errs=$(grep -cE "Traceback|\[ERROR\]|CRITICAL|Exception:" "$LOG" 2>/dev/null | head -1 || true)
errs=${errs:-0}
# Filter out false positives (e.g. "resolved_fail" is not an error)
if [[ "$errs" -gt 0 ]]; then
  real_errs=$(grep -E "Traceback|\[ERROR\]|CRITICAL|Exception:" "$LOG" 2>/dev/null | grep -cvE "resolved_fail|exception_handled|expected_error" || true)
  if [[ "$real_errs" -gt 0 ]]; then
    printf "  ⚠️   Errors/Exceptions           %s\n" "$real_errs"
    echo "  📌 Last error:"
    grep -E "Traceback|\[ERROR\]|CRITICAL|Exception:" "$LOG" | grep -vE "resolved_fail|exception_handled|expected_error" | tail -1 | head -c 120
    echo ""
  else
    echo "  ✅  No errors/exceptions found (filtered)"
  fi
else
  echo "  ✅  No errors/exceptions found"
fi

broken=$(grep -c "BrokenPipe" "$LOG" 2>/dev/null || true)
if [[ "$broken" -gt 0 ]]; then
  printf "  ⚠️   BrokenPipeError             %s\n" "$broken"
fi
echo ""

echo "── Shutdown ──────────────────────────────────────"
check "Clean shutdown"         "stopped\.\|Shutdown signal"
alive=$(grep -c "still alive" "$LOG" 2>/dev/null || true)
if [[ "$alive" -gt 0 ]]; then
  echo "  ⚠️   Arms still alive after shutdown detected"
fi
echo ""

echo "════════════════════════════════════════════════════"
echo "  Health check complete."
echo "════════════════════════════════════════════════════"
