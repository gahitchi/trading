"""
Hypothesis H1 (trend following):
    "When a fast moving average crosses above a slow one, price tends to keep
     rising long enough to beat buy-and-hold after fees."

Entry: fast SMA crosses above slow SMA.
Exit:  fast SMA crosses below slow SMA.

Stoploss and ROI exits are switched off on purpose so the test isolates the
crossover signal itself. Risk management is a separate experiment.
"""

import talib.abstract as ta
from pandas import DataFrame

from technical import qtpylib
from freqtrade.strategy import IntParameter, IStrategy

from lab_safety import assert_paper_only


class SmaCrossover(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False

    minimal_roi = {"0": 1000}
    stoploss = -0.99
    use_exit_signal = True
    process_only_new_candles = True
    startup_candle_count = 200

    # Tunable with hyperopt, but only ever on the TRAINING period (see README).
    fast_period = IntParameter(5, 50, default=20, space="buy")
    slow_period = IntParameter(60, 200, default=100, space="buy")

    def bot_start(self, **kwargs) -> None:
        assert_paper_only(self.config)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def _smas(self, dataframe: DataFrame):
        fast = ta.SMA(dataframe, timeperiod=self.fast_period.value)
        slow = ta.SMA(dataframe, timeperiod=self.slow_period.value)
        return fast, slow

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        fast, slow = self._smas(dataframe)
        dataframe.loc[
            qtpylib.crossed_above(fast, slow) & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        fast, slow = self._smas(dataframe)
        dataframe.loc[
            qtpylib.crossed_below(fast, slow) & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe
