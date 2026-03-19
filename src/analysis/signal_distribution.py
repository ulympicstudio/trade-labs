"""
Signal Distribution Analyzer — observe-only metric collection for threshold calibration.

Collects distributions of signal and filter metrics at each pipeline stage during a
live session. On shutdown, writes structured JSON (and optional CSV) for post-run analysis.

Usage:
    analyzer = SignalDistributionAnalyzer(session_id="20260316T140000")
    analyzer.record_checked(sym, cat_score, qm)     # after quant verification
    analyzer.record_signal(sym, unified, cat, quant, gate, qm)  # after score gate pass
    analyzer.record_intent(sym, entry, risk_pct, qm) # after sizing
    analyzer.record_fill(sym, entry, qty)            # after confirmed fill
    analyzer.print_summary()
    analyzer.write_json("logs/")
    analyzer.write_csv("logs/")                      # optional
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ── Histogram bucket definitions ────────────────────────────────

UNIFIED_BUCKETS = [
    ("<30", 0, 30), ("30-40", 30, 40), ("40-50", 40, 50),
    ("50-60", 50, 60), ("60-70", 60, 70), ("70-80", 70, 80), ("80+", 80, 200),
]
CATALYST_BUCKETS = [
    ("<40", 0, 40), ("40-50", 40, 50), ("50-60", 50, 60),
    ("60-70", 60, 70), ("70-80", 70, 80), ("80-90", 80, 90), ("90+", 90, 200),
]
QUANT_BUCKETS = [
    ("<30", 0, 30), ("30-40", 30, 40), ("40-50", 40, 50),
    ("50-60", 50, 60), ("60-70", 60, 70), ("70+", 70, 200),
]
VOL_ACCEL_BUCKETS = [
    ("<0.8", 0, 0.8), ("0.8-1.0", 0.8, 1.0), ("1.0-1.15", 1.0, 1.15),
    ("1.15-1.3", 1.15, 1.3), ("1.3-2.0", 1.3, 2.0), ("2.0+", 2.0, 100),
]
ATR_PCT_BUCKETS = [
    ("<0.5%", 0, 0.005), ("0.5-1%", 0.005, 0.01), ("1-2%", 0.01, 0.02),
    ("2-3%", 0.02, 0.03), ("3-5%", 0.03, 0.05), ("5%+", 0.05, 1.0),
]


def _bucket(value: float, buckets: List[Tuple[str, float, float]]) -> str:
    for label, low, high in buckets:
        if low <= value < high:
            return label
    return buckets[-1][0]  # overflow into last


def _pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}%" if total > 0 else "0%"


# ── Observation records ─────────────────────────────────────────

@dataclass
class MetricSnapshot:
    """One candidate's metrics at a single pipeline stage."""
    symbol: str
    catalyst_score: float = 0.0
    quant_score: float = 0.0
    unified_score: float = 0.0
    vol_accel: float = 0.0
    atr_pct: float = 0.0
    momentum_30m: float = 0.0
    rs_30m_delta: float = 0.0
    adv20_dollars: float = 0.0
    entry_price: float = 0.0
    risk_pct: float = 0.0
    gate: str = ""


# ── Near-miss thresholds ────────────────────────────────────────

@dataclass
class NearMissZone:
    metric: str
    threshold: float
    zone_low: float
    zone_high: float
    count: int = 0


# ── Main analyzer ───────────────────────────────────────────────

class SignalDistributionAnalyzer:
    """Observe-only metric distribution collector for threshold calibration."""

    def __init__(self, session_id: str = "",
                 unified_threshold: float = 70.0,
                 vol_accel_threshold: float = 1.15,
                 atr_pct_threshold: float = 0.008,
                 rs_threshold: float = 0.0025):
        self.session_id = session_id

        # Configurable thresholds for near-miss analysis
        self._thresholds = {
            "unified_score": unified_threshold,
            "vol_accel": vol_accel_threshold,
            "atr_pct": atr_pct_threshold,
            "rs_30m_delta": rs_threshold,
        }

        # Per-stage observation lists
        self.checked: List[MetricSnapshot] = []
        self.signals: List[MetricSnapshot] = []
        self.intents: List[MetricSnapshot] = []
        self.fills: List[MetricSnapshot] = []

    # ── Recording API ───────────────────────────────────────────

    def record_checked(self, sym: str, cat_score: float, qm) -> None:
        """Record a candidate that reached quant verification."""
        self.checked.append(MetricSnapshot(
            symbol=sym,
            catalyst_score=cat_score,
            quant_score=getattr(qm, "quant_score", 0.0),
            vol_accel=getattr(qm, "volume_accel", 0.0),
            atr_pct=getattr(qm, "atr_percent", 0.0),
            momentum_30m=getattr(qm, "momentum_30m", 0.0),
            rs_30m_delta=getattr(qm, "rel_strength_vs_spy", 0.0),
            adv20_dollars=getattr(qm, "adv20_dollars", 0.0),
        ))

    def record_signal(self, sym: str, unified: float, cat: float,
                      quant: float, gate: str, qm) -> None:
        """Record a candidate that passed all gates (signal generated)."""
        self.signals.append(MetricSnapshot(
            symbol=sym,
            unified_score=unified,
            catalyst_score=cat,
            quant_score=quant,
            gate=gate,
            vol_accel=getattr(qm, "volume_accel", 0.0),
            atr_pct=getattr(qm, "atr_percent", 0.0),
            momentum_30m=getattr(qm, "momentum_30m", 0.0),
            rs_30m_delta=getattr(qm, "rel_strength_vs_spy", 0.0),
            adv20_dollars=getattr(qm, "adv20_dollars", 0.0),
        ))

    def record_intent(self, sym: str, entry: float, risk_pct: float, qm) -> None:
        """Record a trade intent (sized and ready for risk check)."""
        self.intents.append(MetricSnapshot(
            symbol=sym,
            entry_price=entry,
            risk_pct=risk_pct,
            catalyst_score=getattr(qm, "catalyst_score", 0.0) if hasattr(qm, "catalyst_score") else 0.0,
            quant_score=getattr(qm, "quant_score", 0.0),
            vol_accel=getattr(qm, "volume_accel", 0.0),
            atr_pct=getattr(qm, "atr_percent", 0.0),
            momentum_30m=getattr(qm, "momentum_30m", 0.0),
            rs_30m_delta=getattr(qm, "rel_strength_vs_spy", 0.0),
            adv20_dollars=getattr(qm, "adv20_dollars", 0.0),
        ))

    def record_fill(self, sym: str, entry: float, qty: int) -> None:
        """Record a confirmed fill."""
        self.fills.append(MetricSnapshot(
            symbol=sym,
            entry_price=entry,
        ))

    # ── Histogram builders ──────────────────────────────────────

    @staticmethod
    def _histogram(values: List[float], buckets) -> Dict[str, int]:
        hist = {label: 0 for label, _, _ in buckets}
        for v in values:
            hist[_bucket(v, buckets)] += 1
        return hist

    def _stage_histograms(self, stage: List[MetricSnapshot]) -> dict:
        if not stage:
            return {}
        return {
            "unified_score": self._histogram(
                [s.unified_score for s in stage if s.unified_score > 0], UNIFIED_BUCKETS),
            "catalyst_score": self._histogram(
                [s.catalyst_score for s in stage if s.catalyst_score > 0], CATALYST_BUCKETS),
            "quant_score": self._histogram(
                [s.quant_score for s in stage if s.quant_score > 0], QUANT_BUCKETS),
            "vol_accel": self._histogram(
                [s.vol_accel for s in stage if s.vol_accel > 0], VOL_ACCEL_BUCKETS),
            "atr_pct": self._histogram(
                [s.atr_pct for s in stage if s.atr_pct > 0], ATR_PCT_BUCKETS),
        }

    # ── Near-miss analysis ──────────────────────────────────────

    def _near_misses(self, stage: List[MetricSnapshot]) -> List[dict]:
        """Count candidates that were just below a threshold."""
        zones = [
            NearMissZone("unified_score",
                         self._thresholds["unified_score"],
                         self._thresholds["unified_score"] - 10,
                         self._thresholds["unified_score"]),
            NearMissZone("vol_accel",
                         self._thresholds["vol_accel"],
                         self._thresholds["vol_accel"] - 0.25,
                         self._thresholds["vol_accel"]),
            NearMissZone("atr_pct",
                         self._thresholds["atr_pct"],
                         self._thresholds["atr_pct"] - 0.003,
                         self._thresholds["atr_pct"]),
            NearMissZone("rs_30m_delta",
                         self._thresholds["rs_30m_delta"],
                         self._thresholds["rs_30m_delta"] - 0.002,
                         self._thresholds["rs_30m_delta"]),
        ]
        for snap in stage:
            for z in zones:
                val = getattr(snap, z.metric, None)
                if val is not None and z.zone_low <= val < z.zone_high:
                    z.count += 1
        return [
            {
                "metric": z.metric,
                "threshold": z.threshold,
                "near_miss_range": f"[{z.zone_low}, {z.zone_high})",
                "count": z.count,
            }
            for z in zones if z.count > 0
        ]

    # ── Summary Stats ───────────────────────────────────────────

    @staticmethod
    def _stats(values: List[float]) -> dict:
        clean = [v for v in values if math.isfinite(v)]
        if not clean:
            return {"count": 0, "min": 0, "max": 0, "mean": 0,
                    "median": 0, "p25": 0, "p75": 0, "stdev": 0}
        sorted_v = sorted(clean)
        n = len(sorted_v)
        return {
            "count": n,
            "min": round(sorted_v[0], 4),
            "max": round(sorted_v[-1], 4),
            "mean": round(statistics.mean(sorted_v), 4),
            "median": round(statistics.median(sorted_v), 4),
            "p25": round(sorted_v[max(0, n // 4 - 1)], 4) if n >= 4 else round(sorted_v[0], 4),
            "p75": round(sorted_v[min(n - 1, 3 * n // 4)], 4) if n >= 4 else round(sorted_v[-1], 4),
            "stdev": round(statistics.stdev(sorted_v), 4) if n >= 2 else 0,
        }

    def _stage_stats(self, stage: List[MetricSnapshot]) -> dict:
        if not stage:
            return {}
        return {
            "unified_score": self._stats([s.unified_score for s in stage if s.unified_score > 0]),
            "catalyst_score": self._stats([s.catalyst_score for s in stage if s.catalyst_score > 0]),
            "quant_score": self._stats([s.quant_score for s in stage if s.quant_score > 0]),
            "vol_accel": self._stats([s.vol_accel for s in stage]),
            "atr_pct": self._stats([s.atr_pct for s in stage]),
            "momentum_30m": self._stats([s.momentum_30m for s in stage]),
            "rs_30m_delta": self._stats([s.rs_30m_delta for s in stage]),
            "adv20_dollars": self._stats([s.adv20_dollars for s in stage]),
        }

    # ── Full payload builder ────────────────────────────────────

    def build(self) -> dict:
        """Build the complete distribution analysis payload."""
        checked_above = sum(
            1 for s in self.checked
            if s.unified_score >= self._thresholds["unified_score"]
        )
        # For checked candidates, unified isn't set yet — use a proxy:
        # count how many have vol_accel above threshold as "would-pass-hyper"
        hyper_passable = sum(
            1 for s in self.checked
            if s.vol_accel >= self._thresholds["vol_accel"]
        )

        return {
            "session_id": self.session_id,
            "thresholds": self._thresholds,
            "stage_counts": {
                "checked": len(self.checked),
                "signals": len(self.signals),
                "intents": len(self.intents),
                "fills": len(self.fills),
            },
            "conversion_rates": {
                "checked_to_signal": _pct(len(self.signals), len(self.checked)),
                "signal_to_intent": _pct(len(self.intents), len(self.signals)),
                "intent_to_fill": _pct(len(self.fills), len(self.intents)),
                "checked_to_fill": _pct(len(self.fills), len(self.checked)),
            },
            "checked": {
                "stats": self._stage_stats(self.checked),
                "histograms": self._stage_histograms(self.checked),
                "near_misses": self._near_misses(self.checked),
                "hyper_passable_pct": _pct(hyper_passable, len(self.checked)),
            },
            "signals": {
                "stats": self._stage_stats(self.signals),
                "histograms": self._stage_histograms(self.signals),
                "gates_used": dict(Counter(s.gate for s in self.signals)),
            },
            "intents": {
                "stats": self._stage_stats(self.intents),
                "entry_price": self._stats([s.entry_price for s in self.intents]),
                "risk_pct": self._stats([s.risk_pct for s in self.intents]),
            },
            "fills": {
                "count": len(self.fills),
                "entry_price": self._stats([s.entry_price for s in self.fills]),
            },
        }

    # ── Console summary ─────────────────────────────────────────

    def print_summary(self) -> None:
        """Print a concise distribution summary to console."""
        data = self.build()
        sc = data["stage_counts"]
        cr = data["conversion_rates"]

        print("\n" + "=" * 64)
        print("  SIGNAL DISTRIBUTION SUMMARY")
        print("=" * 64)
        print(f"  session_id : {self.session_id}")
        print(f"  pipeline   : checked={sc['checked']} → signals={sc['signals']} "
              f"→ intents={sc['intents']} → fills={sc['fills']}")
        print(f"  conversion : checked→signal={cr['checked_to_signal']}  "
              f"signal→intent={cr['signal_to_intent']}  "
              f"intent→fill={cr['intent_to_fill']}")
        print("-" * 64)

        # Signals stats
        if self.signals:
            ss = data["signals"]["stats"]
            u = ss.get("unified_score", {})
            print("  SIGNAL SCORES (passed all gates)")
            print(f"    unified  : median={u.get('median', 0):.1f}  "
                  f"p75={u.get('p75', 0):.1f}  "
                  f"min={u.get('min', 0):.1f}  max={u.get('max', 0):.1f}  "
                  f"n={u.get('count', 0)}")
            gates = data["signals"].get("gates_used", {})
            if gates:
                gate_str = "  ".join(f"{g}={n}" for g, n in gates.items())
                print(f"    gates    : {gate_str}")
            print("-" * 64)

        # Checked stats (full funnel)
        if self.checked:
            cs = data["checked"]["stats"]
            va = cs.get("vol_accel", {})
            ap = cs.get("atr_pct", {})
            print("  CHECKED CANDIDATES (quant-verified)")
            print(f"    vol_accel: median={va.get('median', 0):.2f}  "
                  f"p75={va.get('p75', 0):.2f}  "
                  f"n={va.get('count', 0)}")
            print(f"    atr_pct  : median={ap.get('median', 0):.4f}  "
                  f"p75={ap.get('p75', 0):.4f}")
            hp = data["checked"].get("hyper_passable_pct", "0%")
            print(f"    above vol_accel threshold: {hp}")
            print("-" * 64)

        # Near misses
        if self.checked:
            nm = data["checked"].get("near_misses", [])
            if nm:
                print("  NEAR-MISS ZONES (just below threshold)")
                for z in nm:
                    print(f"    {z['metric']}: {z['count']} in {z['near_miss_range']}  "
                          f"(threshold={z['threshold']})")
                print("-" * 64)

        # Intents
        if self.intents:
            rp = data["intents"].get("risk_pct", {})
            ep = data["intents"].get("entry_price", {})
            print("  TRADE INTENTS")
            print(f"    risk_pct   : median={rp.get('median', 0):.4f}  "
                  f"max={rp.get('max', 0):.4f}")
            print(f"    entry_price: median=${ep.get('median', 0):.2f}  "
                  f"range=${ep.get('min', 0):.2f}-${ep.get('max', 0):.2f}")
            print("-" * 64)

        print("=" * 64)

    # ── File output ─────────────────────────────────────────────

    def write_json(self, output_dir: str = "logs") -> Optional[Path]:
        """Write distribution JSON. Returns path written or None."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        payload = self.build()
        fpath = out / f"signal_distribution_{self.session_id}.json"
        canonical = out / "signal_distribution.json"
        try:
            for p in (fpath, canonical):
                with open(p, "w") as f:
                    json.dump(payload, f, indent=2, default=str)
            return fpath
        except Exception as e:
            print(f"[ERROR] Failed to write signal distribution: {e}")
            return None

    def write_csv(self, output_dir: str = "logs") -> Optional[Path]:
        """Write flat CSV of all checked candidates for easy review."""
        if not self.checked:
            return None
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        fpath = out / f"signal_distribution_{self.session_id}.csv"
        fields = [
            "stage", "symbol", "catalyst_score", "quant_score", "unified_score",
            "vol_accel", "atr_pct", "momentum_30m", "rs_30m_delta",
            "adv20_dollars", "entry_price", "risk_pct", "gate",
        ]
        try:
            with open(fpath, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                for stage_name, stage_list in [
                    ("checked", self.checked), ("signal", self.signals),
                    ("intent", self.intents), ("fill", self.fills),
                ]:
                    for s in stage_list:
                        writer.writerow({
                            "stage": stage_name,
                            "symbol": s.symbol,
                            "catalyst_score": round(s.catalyst_score, 1),
                            "quant_score": round(s.quant_score, 1),
                            "unified_score": round(s.unified_score, 1),
                            "vol_accel": round(s.vol_accel, 3),
                            "atr_pct": round(s.atr_pct, 5),
                            "momentum_30m": round(s.momentum_30m, 5),
                            "rs_30m_delta": round(s.rs_30m_delta, 5),
                            "adv20_dollars": round(s.adv20_dollars, 0),
                            "entry_price": round(s.entry_price, 2),
                            "risk_pct": round(s.risk_pct, 5),
                            "gate": s.gate,
                        })
            return fpath
        except Exception as e:
            print(f"[ERROR] Failed to write signal distribution CSV: {e}")
            return None
