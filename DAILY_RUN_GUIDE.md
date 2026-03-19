# Trade Labs — Daily Run Guide

## Quick Start (Daily)

```bash
cd ~/trade-labs
set -a && source .env && set +a
python -u -m src.arms.dev_all_in_one 2>&1 | tee -a /tmp/tradelabs_daily.log
```

## Timed Run (3 minutes)

```bash
cd ~/trade-labs
set -a && source .env && set +a
timeout 180 python -u -m src.arms.dev_all_in_one 2>&1 | tee /tmp/tradelabs_timed.log
```

## Production Run (no debug noise)

```bash
cd ~/trade-labs
set -a && source .env && set +a
TL_NEWS_CONSENSUS_DEBUG=0 \
TL_NEWS_CANONICALIZE_GNEWS=false \
python -u -m src.arms.dev_all_in_one 2>&1 | tee -a /tmp/tradelabs_daily.log
```

## With iMessage Alerts

```bash
cd ~/trade-labs
set -a && source .env && set +a
TL_IMESSAGE_ENABLED=true \
TL_IMESSAGE_TARGET="+1XXXXXXXXXX" \
python -u -m src.arms.dev_all_in_one 2>&1 | tee -a /tmp/tradelabs_daily.log
```

## Health Check

After running (or while running in another terminal):

```bash
./scripts/check_system.sh /tmp/tradelabs_daily.log
```

## Notes

- Always use `python -u` for unbuffered stdout (prevents log gaps when piping)
- `set -a && source .env && set +a` exports all `.env` vars to the environment
- Use `tee -a` to **append** to the log file across multiple runs
- Press `Ctrl+C` for clean shutdown; all arms will stop within ~5 seconds
- Clear stale bytecode before first run after code changes: `rm -rf src/arms/__pycache__`

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `TL_NEWS_PROVIDERS` | `benzinga,gnews` | Comma-separated news providers |
| `TL_NEWS_CONSENSUS_DEBUG` | `false` | Enable verbose consensus debug logging |
| `TL_NEWS_CANONICALIZE_GNEWS` | `false` | Enable GNews URL canonicalization attempts |
| `TL_NEWS_CONSENSUS_BOOST` | `2` | Impact score boost per additional provider |
| `TL_SIG_COOLDOWN_S` | `60` | Base cooldown between intents per symbol (seconds) |
| `TL_SIG_COOLDOWN_CONSENSUS_S` | `300` | Cooldown after consensus-driven intent |
| `TL_SIG_CONSENSUS_CONF_BOOST` | `0.15` | Confidence boost for consensus events |
| `TL_IMESSAGE_ENABLED` | `false` | Enable iMessage alerts for consensus events |
| `TL_IMESSAGE_TARGET` | *(empty)* | Phone number or Apple ID for iMessage |
| `TL_IMESSAGE_COOLDOWN_S` | `300` | Min seconds between iMessage alerts per symbol |
| `TL_ESCALATION_ENABLED` | `false` | Enable risk escalation (size up on high impact) |
| `TL_ESCALATION_IMPACT_MIN` | `6` | Min impact_score to trigger escalation |
