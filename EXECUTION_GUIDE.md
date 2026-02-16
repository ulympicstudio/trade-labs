# Trade Labs - Buy/Sell Execution Guide
**How to Arm and Disarm Trading**

---

## 🎯 IMPORTANT: Trade Labs is a SCANNING Tool

**Trade Labs does NOT automatically trade for you.**

It works like this:
1. **You run a scan** → System finds opportunities
2. **You review results** → Decide which trades to take
3. **You manually execute** → Place orders in TWS yourself
4. **You manage exits** → Monitor and close positions

**Think of it as your research assistant, not an autopilot.**

---

## 📋 STEP-BY-STEP: From Scan to Execution

### STEP 1: Run a Scan

```bash
cd /Users/umronalkotob/trade-labs
python run_hybrid_trading.py
```

**Output Example:**
```
Rank  Symbol  Total   Entry     Stop      Target    Shares  Investment
1     AVGO    78.5    $175.20   $171.85   $183.60   150     $26,280
2     VST     74.3    $142.50   $139.10   $151.00   195     $27,788
3     TRI     71.8    $88.40    $86.50    $93.15    325     $28,730
```

---

### STEP 2: Review and Select Trades

**Ask yourself:**
- ✅ Do I agree with the entry price?
- ✅ Am I comfortable with the stop loss?
- ✅ Is the target realistic?
- ✅ Can I afford this investment amount?
- ✅ Does this fit my portfolio?

**Select 5-10 trades from the top of the list** (don't take all 50)

---

### STEP 3: Place Orders in TWS

#### A. Open TWS Order Entry

1. In TWS, click **"Symbol"** field
2. Type the symbol (e.g., **AVGO**)
3. Press Enter

#### B. Set Up the Order

**For Entry (Buy Order):**

1. **Action:** BUY
2. **Quantity:** [From scan results - e.g., 150 shares]
3. **Order Type:** LIMIT
4. **Limit Price:** [Entry price from scan - e.g., $175.20]
5. **Time in Force:** DAY
6. Click **"Transmit"**

**Example:**
```
Symbol:    AVGO
Action:    BUY
Quantity:  150
Type:      LIMIT
Price:     $175.20
TIF:       DAY
```

#### C. Set Stop Loss (After Filled)

Once your buy order fills:

1. Right-click the position in your portfolio
2. Select **"Attach Order" → "Stop"**
3. **Action:** SELL
4. **Quantity:** [Same as position]
5. **Order Type:** STOP
6. **Stop Price:** [Stop from scan - e.g., $171.85]
7. **Time in Force:** GTC (Good Till Cancelled)
8. Click **"Transmit"**

#### D. Set Profit Target (Optional)

1. Right-click the position
2. Select **"Attach Order" → "Limit"**
3. **Action:** SELL
4. **Quantity:** [Same as position]
5. **Order Type:** LIMIT
6. **Limit Price:** [Target from scan - e.g., $183.60]
7. **Time in Force:** GTC
8. Click **"Transmit"**

**Now you have:**
- ✅ Position entered at entry price
- ✅ Stop loss protecting you
- ✅ Target order waiting to take profit

---

### STEP 4: Track Your Positions

Create a simple spreadsheet:

| Symbol | Entry Date | Shares | Entry $ | Stop $ | Target $ | Status |
|--------|-----------|--------|---------|--------|----------|--------|
| AVGO   | 2/14/26   | 150    | $175.20 | $171.85| $183.60  | Open   |
| VST    | 2/14/26   | 195    | $142.50 | $139.10| $151.00  | Open   |

**Or use the scan output file:**
- Saved as: `hybrid_scan_YYYYMMDD_HHMMSS.json`
- Contains all your selected trades

---

### STEP 5: Manage Exits

#### When Stop is Hit
- TWS automatically sells at your stop price
- You're out of the trade (protected from bigger loss)
- Update tracking: Status = "Stopped"

#### When Target is Hit
- TWS automatically sells at your target price
- You took profit (2.5x your risk)
- Update tracking: Status = "Target"

#### Manual Exit (Your Decision)
If price behavior changes:

1. In TWS, right-click position
2. Select **"Close Position"**
3. Choose MARKET or LIMIT
4. Click **"Transmit"**
5. Update tracking: Status = "Manual Exit"

---

## 🔐 ARMING/DISARMING EXPLAINED

### "ARMED" = Ready to Execute Trades

**What "Armed" means:**
- ✅ You've run a scan
- ✅ You've reviewed results
- ✅ You're ready to place orders in TWS
- ✅ TWS is open and logged in
- ✅ You have capital available

**To ARM (Prepare for Trading):**
1. Start TWS and log in
2. Navigate to Trade Labs folder
3. Run scan: `python run_hybrid_trading.py`
4. Review approved positions
5. Have TWS order entry ready

**Status: ARMED** - You're ready to execute

---

### "DISARMED" = Not Trading

**What "Disarmed" means:**
- ❌ You're not scanning for new trades
- ❌ You're not placing new orders
- ❌ You're only managing existing positions (if any)

**To DISARM (Stop New Trading):**
1. Don't run any more scans
2. Finish managing current positions
3. Let stops/targets work
4. Close TWS when all positions are closed

**Status: DISARMED** - No new trades

---

## ⚡ QUICK REFERENCE: Trading Workflow

### Morning Routine (ARMED)

**8:00 AM - Pre-Market:**
```bash
./morning_scan.sh
```

1. System scans market (60-90 seconds)
2. Shows 10-50 approved opportunities
3. You review the list
4. You select 5-10 best trades
5. You place orders in TWS (limit orders at entry prices)

**9:30 AM - Market Open:**
- Your limit orders fill (or don't)
- For filled orders: attach stops and targets immediately
- Update your tracking spreadsheet

**During Day:**
- Let stops/targets work automatically
- Check TWS periodically
- React to any manual exit signals

**3:30 PM - End of Day:**
- Review open positions
- Check if any need adjustment
- Plan for tomorrow

---

### Non-Trading Mode (DISARMED)

**Don't run scans**
- No `python run_hybrid_trading.py`
- No new positions

**Only manage what's open:**
- Monitor existing stops/targets
- Let positions exit naturally
- Or manually close if needed

**When all positions are closed:**
- System is fully disarmed
- No active risk

---

## 🛡️ SAFETY CONTROLS

### Rule 1: Start Small
- First week: max 3-5 positions
- Second week: max 10 positions
- Once comfortable: scale to 20-30

### Rule 2: Never Override Stops
- If stop is hit, accept it
- Don't "hope" it comes back
- Cut losers fast

### Rule 3: Don't Trade Everything
- Scan might show 50 opportunities
- Only take top 10 you really like
- Quality over quantity

### Rule 4: Check Before Trading
Always run preflight check:
```bash
python preflight_check.py
```

### Rule 5: Manual Review Required
- Never blindly execute all results
- Always review each trade
- Trust your judgment

---

## 📊 EXAMPLE: Full Trading Cycle

### Monday 8:00 AM - SCAN
```bash
python run_hybrid_trading.py
```

**Results:**
- AVGO: 78.5 score, entry $175.20, stop $171.85, 150 shares
- VST: 74.3 score, entry $142.50, stop $139.10, 195 shares

**Decision:** Take both trades

---

### Monday 9:30 AM - EXECUTE

**In TWS:**

**Trade 1:**
- BUY 150 AVGO at $175.20 LIMIT
- [Order fills at $175.18] ✅

**Attach Stop:**
- SELL 150 AVGO at $171.85 STOP GTC ✅

**Attach Target:**
- SELL 150 AVGO at $183.60 LIMIT GTC ✅

**Trade 2:**
- BUY 195 VST at $142.50 LIMIT
- [Order fills at $142.48] ✅

**Attach Stop:**
- SELL 195 VST at $139.10 STOP GTC ✅

**Attach Target:**
- SELL 195 VST at $151.00 LIMIT GTC ✅

**Status:** 2 positions open, protected with stops

---

### Tuesday - MANAGE

**AVGO:**
- Trading at $178.50 (up from $175.18)
- Stop still at $171.85
- Target at $183.60
- Action: None (let it run)

**VST:**
- Trading at $140.20 (down from $142.48)
- Stop at $139.10
- Action: None (stop will protect)

---

### Wednesday - TARGET HIT

**AVGO:**
- Hits $183.60 target ✅
- Automatic sell at $183.60
- Profit: ($183.60 - $175.18) × 150 = $1,263
- **Close trade in tracking sheet**

**VST:**
- Still at $140.50
- Keep monitoring

---

### Thursday - STOP HIT

**VST:**
- Drops to $139.10
- Stop triggered ✅
- Automatic sell at $139.10
- Loss: ($139.10 - $142.48) × 195 = -$659
- **Close trade in tracking sheet**

---

### Friday - ALL POSITIONS CLOSED

**Summary:**
- AVGO: +$1,263 (winner)
- VST: -$659 (loser)
- Net: +$604

**Status:** DISARMED (no positions)

**Next:** Run new scan Monday if desired

---

## 🎛️ Advanced: Bracket Orders (One-Click Entry+Stops)

### What is a Bracket Order?
A bracket order enters position + stop + target in ONE order.

### How to Set Up in TWS:

1. Click **"Order Type"** → Select **"Bracket"**
2. **Parent Order:**
   - Action: BUY
   - Quantity: [From scan]
   - Type: LIMIT
   - Price: [Entry price]

3. **Stop Loss Child:**
   - Automatically creates STOP order
   - Set price to: [Stop price from scan]

4. **Profit Target Child:**
   - Automatically creates LIMIT order
   - Set price to: [Target price from scan]

5. Click **"Transmit"**

**Result:** One click = entry + stop + target all set up ✅

---

## 🚨 EMERGENCY: Close Everything

### If You Need to Exit All Positions Immediately:

**In TWS:**
1. Go to **Portfolio** view
2. Select all open positions (Cmd+A on Mac)
3. Right-click → **"Close Selected Positions"**
4. Choose **MARKET** for instant exit
5. Click **"Transmit All"**

**All positions will close at market price within seconds.**

---

## 📞 QUICK COMMAND REFERENCE

| Action | Command |
|--------|---------|
| **Scan for trades** | `python run_hybrid_trading.py` |
| **Check system** | `python preflight_check.py` |
| **See what's trending** | `python test_news_integration.py` |
| **Find earnings plays** | `python find_earnings.py` |
| **Execute trades** | Manually in TWS |
| **Close all positions** | TWS → Close Selected Positions |

---

## 💡 PRO TIPS

### Tip 1: Use TWS Mobile App
- Monitor positions from phone
- Get alerts when stops/targets hit
- Manage on the go

### Tip 2: Set Price Alerts
In TWS:
- Right-click symbol
- "Create Alert"
- Set at your stop and target prices
- Get notified when hit

### Tip 3: Keep a Trading Journal
After each closed trade:
- What was the setup?
- Did it hit target or stop?
- What did you learn?
- Would you take it again?

### Tip 4: Don't Revenge Trade
If stopped out:
- Don't immediately re-enter
- Wait for next scan
- Fresh opportunity is better

### Tip 5: Scale In (Advanced)
Instead of full position at once:
- Buy 50% at entry price
- If moves in your favor, add 50% more
- Reduces risk of bad timing

---

## ✅ CHECKLIST: Before Each Trading Session

**Pre-Trading (ARMING):**
- [ ] TWS is open and logged in
- [ ] API is enabled in TWS settings
- [ ] Run `python preflight_check.py` (all systems go)
- [ ] Run `python run_hybrid_trading.py` (get opportunities)
- [ ] Review results carefully
- [ ] Select 5-10 best trades
- [ ] Have TWS order entry ready

**During Trading:**
- [ ] Place entry orders (limit orders)
- [ ] Attach stops immediately after fills
- [ ] Attach targets (optional)
- [ ] Update tracking spreadsheet
- [ ] Set price alerts

**Post-Trading (DISARMING):**
- [ ] Review what filled
- [ ] Check all stops are in place
- [ ] Update journal
- [ ] Plan for tomorrow

---

## 🎯 BOTTOM LINE

**Trade Labs DOES:**
✅ Scan and analyze market  
✅ Find best opportunities  
✅ Calculate entry/stop/target  
✅ Suggest position sizes  
✅ Rank by quality

**Trade Labs DOES NOT:**
❌ Automatically place orders  
❌ Execute trades for you  
❌ Manage positions automatically  
❌ Close positions for you

**YOU must:**
👉 Review results  
👉 Decide what to trade  
👉 Place orders in TWS  
👉 Manage exits  
👉 Track performance

---

## 📚 Think of It Like This:

**Trade Labs = Your Research Analyst**
- Does all the hard work
- Gives you a ranked list
- "Here are the best 50 opportunities"

**You = The Portfolio Manager**
- Reviews the list
- Makes decisions
- "I'll take these 10"
- Executes trades
- Manages risk

**TWS = Your Order Desk**
- Where you actually trade
- Place orders
- Monitor positions
- Execute stops/targets

---

**Remember: You're always in control. Trade Labs suggests, you decide and execute.**

