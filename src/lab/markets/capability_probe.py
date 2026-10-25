"""Probe exports from the *installed/pinned* NautilusTrader wheel, not latest docs.

Read-only: no trading, keys, sockets or account connection. Invoked explicitly
by CI after uv sync --frozen and available through python -m lab.markets.capability_probe.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json


def inspect_adapters() -> dict:
    result = {"nautilus_trader_version": importlib.metadata.version("nautilus_trader"), "adapters": {}}
    for name, symbols in {
        "okx": ("OKXRegion", "OKXEnvironment", "OKXDataClientConfig", "OKXExecClientConfig", "OKXExecutionClientConfig"),
        "alpaca": ("AlpacaDataClientConfig", "AlpacaExecClientConfig"),
        "interactive_brokers": ("InteractiveBrokersDataClientConfig", "InteractiveBrokersExecutionClientConfig"),
        "ctrader": (),
    }.items():
        try:
            module = importlib.import_module(f"nautilus_trader.adapters.{name}")
        except (ImportError, AttributeError, OSError) as exc:
            result["adapters"][name] = {"importable": False, "error": type(exc).__name__}
            continue
        export_names = set(dir(module))
        data = {
            "importable": True,
            "requested_symbols": {s: s in export_names for s in symbols},
            "client_exports": sorted(s for s in export_names if "Client" in s and ("Config" in s or "Factory" in s)),
        }
        for enum_name in ("OKXRegion", "OKXEnvironment"):
            enum = getattr(module, enum_name, None)
            if enum is not None:
                data[f"{enum_name}_members"] = [s for s in ("EEA", "EU", "DEMO", "LIVE") if hasattr(enum, s)]
        result["adapters"][name] = data
    return result


def main() -> None:
    print(json.dumps(inspect_adapters(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
