"""Write a reviewable bank template bound to exact configured strategy artifacts."""

import argparse
from pathlib import Path

from lab.authority import digest
from lab.config import load_config
from lab.persistence import atomic_json
from lab.signals.registry import resolve
from lab.strategy_bank import StrategyBank


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--signals", nargs="+", required=True)
    p.add_argument("--tickers", nargs="+", required=True)
    p.add_argument("--benchmark", required=True)
    p.add_argument("--eligible-after", required=True)
    p.add_argument("--expires-at", required=True)
    p.add_argument("--config", type=Path, default=Path("lab.toml"))
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        p.error("refusing to overwrite an existing policy")
    cfg = load_config(a.config)
    specs = [resolve(n, cfg.research_dir, require_tradable=True) for n in a.signals]
    manifest = {
        "version": 1,
        "selection": "fixed_priority_regime_v1",
        "benchmark": a.benchmark,
        "window": 63,
        "high_volatility": 0.30,
        "trend_band": 0.03,
        # Regime persistence against chatter around a threshold (Gemini audit F-R17): leave "volatile" below
        # 85% of the entry level, a trend below 75% of it, and follow a new regime after 3 bars.
        "volatility_exit": 0.85,
        "trend_exit": 0.75,
        "min_dwell_bars": 3,
        "entries": [
            {
                "name": s.name,
                "artifact": digest({"name": s.name, "params": s.params, "sha256": s.sha256}),
                "priority": i,
                "regimes": ["up", "down", "range", "volatile"],
                "eligible_after": a.eligible_after,
                "expires_at": a.expires_at,
            }
            for i, s in enumerate(specs)
        ],
    }
    StrategyBank(manifest, specs, a.tickers)
    atomic_json(a.output, manifest)
    print(f"{a.output}: template only; review regime assignments and qualify the complete policy before use")


if __name__ == "__main__":
    main()
