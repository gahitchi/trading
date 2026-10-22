"""
Hypothesis H2 (mean reversion):
    "After price is oversold (low RSI), it tends to bounce back enough to beat
     buy-and-hold after fees."

Entry: RSI crosses back ABOVE the oversold level (bounce confirmation).
Exit:  RSI crosses above the overbought level.

Stoploss is switched off on purpose to isolate the signal. Expect this to
reveal the classic weakness of mean reversion: in a sustained downtrend the
"bounce" never comes and the position bleeds. Adding a stoploss is a
separate experiment.
"""

import talib.abstract as ta
from pandas import DataFrame

from technical import qtpylib
from freqtrade.strategy import IntParameter, IStrategy

from lab_safety import assert_paper_only


class RsiMeanReversion(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False

    minimal_roi = {"0": 1000}
    stoploss = -0.99
    use_exit_signal = True
    process_only_new_candles = True
    startup_candle_count = 50

    rsi_period = IntParameter(7, 28, default=14, space="buy")
    oversold = IntParameter(15, 40, default=30, space="buy")
    overbought = IntParameter(60, 85, default=70, space="sell")

    def bot_start(self, **kwargs) -> None:
        assert_paper_only(self.config)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        rsi = ta.RSI(dataframe, timeperiod=self.rsi_period.value)
        dataframe.loc[
            qtpylib.crossed_above(rsi, self.oversold.value) & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        rsi = ta.RSI(dataframe, timeperiod=self.rsi_period.value)
        dataframe.loc[
            qtpylib.crossed_above(rsi, self.overbought.value) & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe
