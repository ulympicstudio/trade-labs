from config.identity import SYSTEM_NAME, HUMAN_NAME
from src.data.ib_market_data import connect_ib
from src.signals.market_scanner import scan_us_most_active, get_quote, passes_quality_filters


def main():
    print(f"\n{SYSTEM_NAME} → {HUMAN_NAME}: Market Scanner v2 (MOST_ACTIVE + quality filters)\n")

    ib = connect_ib()
    raw = scan_us_most_active(ib, limit=50)

    kept = []
    rejected = 0

    for s in raw:
        bid, ask, last = get_quote(ib, s.symbol)
        ok = passes_quality_filters(
            symbol=s.symbol,
            bid=bid,
            ask=ask,
            last=last,
            min_price=5.0,
            max_spread_pct=0.0015,   # 0.15%
            block_leveraged_etfs=True
        )
        if ok:
            kept.append((s.rank, s.symbol, bid, ask, last))
            if len(kept) >= 15:
                break
        else:
            rejected += 1

    ib.disconnect()

    print(f"Kept {len(kept)} symbols, rejected {rejected} (first 50 scanned)\n")
    for i, (rank, sym, bid, ask, last) in enumerate(kept, start=1):
        price = last if last is not None else (bid + ask) / 2
        spread = (ask - bid) if (bid is not None and ask is not None) else 0.0
        print(f"{i:02d}. {sym} | rank={rank} | price={price:.2f} | spread={spread:.4f}")


if __name__ == "__main__":
    main()
