"""
Baseline (control): buy at the first candle, hold until the end of the test period.

Every other strategy is judged against this. A strategy that trades a lot
but ends below buy-and-hold has not found an edge; it has paid fees to
underperform doing nothing.
"""

from pandas import DataFrame

from freqtrade.strategy import IStrategy

from lab_safety import assert_paper_only


class BuyAndHold(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False

    # Never exit on ROI or stoploss: the position is closed when the backtest ends.
    minimal_roi = {"0": 1000}
    stoploss = -0.99
    use_exit_signal = False
    process_only_new_candles = True
    startup_candle_count = 0

    def bot_start(self, **kwargs) -> None:
        assert_paper_only(self.config)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Always "want" to be in; Freqtrade only opens one trade per pair.
        dataframe.loc[dataframe["volume"] > 0, "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe
