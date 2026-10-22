# Risk-adaptive execution profiles

Status: **profile activation integrated into the configuration loader**; all orders remain restricted to paper accounts. The actual AllocatorAgent and RiskAgent consume the resulting limits in both backtests and paper mode. Autonomy is the operational default (the user starts the paper node), with manual/supervised controls still future work.

Set `[risk] profile = "calm"`, `"cautious"`, `"balanced"`, `"dynamic"` or `"aggressive"` in `lab.toml`. `"custom"` preserves the legacy configuration.

| Profile | Target annual volatility | Maximum gross | Maximum short gross | Max drawdown |
| --- | ---: | ---: | ---: | ---: |
| Calm | 6% | 0.30× | 0× | 8% |
| Cautious | 10% | 0.50× | 0× | 12% |
| Balanced | 14% | 1.00× | 0× | 18% |
| Dynamic | 20% | 1.35× | 0.30× | 25% |
| Aggressive | 28% | 1.80× | 0.75× | 30% |

The profile target volatility is a risk-budget target, **not a forecast or guarantee**. Exposure ceilings, net range, concentration limits and configured broker margin take precedence. The portfolio's positive explicitly configured volatility target is an additional tighter ceiling. For a selected preset, a zero target means use its default rather than disable volatility targeting.

The operator's existing `max_gross`, `max_short_gross`, `max_weight`, `max_drawdown`, `min_net`, `max_net` and portfolio leverage cap are never relaxed. In the checked-in configuration, gross stays at 1× and shorts remain forbidden even under Aggressive. To research leveraged/short portfolios, explicitly configure higher *hard limits*, verify simulated broker permissions, and obtain point-in-time borrow inventory and fees. **Do not interpret a risk profile as proof of borrow availability or real-money authorization.**

The live-node harness must verify any activated short strategies against a simulated broker with an explicit `shortable` set. Real IBKR routing, live borrow availability and account house margins remain unverified until actual paper smoke testing. The system does not automatically change profiles when a model recommends more risk.

Inspect the effective (not hypothetical) limits with `lab risk-profile --json`; this reports the configuration used by real agents and does not submit orders.
