"""
Safety guard for the trading lab.

Every lab strategy calls `assert_paper_only()` from its `bot_start()`.
Backtesting, hyperopt and dry-run (paper trading) are allowed.
Live trading with real money is refused, even if someone flips
"dry_run" to false in the config.

This module defines no strategy class, so Freqtrade ignores it when
listing strategies.
"""

from freqtrade.enums import RunMode
from freqtrade.exceptions import OperationalException

ALLOWED_MODES = {
    RunMode.BACKTEST,
    RunMode.HYPEROPT,
    RunMode.DRY_RUN,
    RunMode.PLOT,
    RunMode.WEBSERVER,
    RunMode.UTIL_EXCHANGE,
    RunMode.UTIL_NO_EXCHANGE,
    RunMode.OTHER,
}


def assert_paper_only(config: dict) -> None:
    runmode = config.get("runmode")
    simulated = runmode in (RunMode.BACKTEST, RunMode.HYPEROPT)
    if runmode == RunMode.LIVE or (not simulated and not config.get("dry_run", False)):
        raise OperationalException(
            "trading-lab is for research only: live trading is disabled. "
            "Run backtests, hyperopt, or dry-run (\"dry_run\": true)."
        )
    if runmode not in ALLOWED_MODES:
        raise OperationalException(f"trading-lab: unexpected run mode {runmode!r}, refusing to start.")
