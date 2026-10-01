# pragma pylint: disable=missing-docstring, invalid-name
from __future__ import annotations

from datetime import datetime
from typing import Optional

import numpy as np
from pandas import DataFrame

import talib.abstract as ta

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative


class MarketOpportunityStrategy(IStrategy):
    """
    Broad-market Binance USDT-M opportunity scanner.

    Design goals:
    - High volatility is a regime, not a prerequisite.
    - Define structure/location first, then compute RR.
    - Require stronger score/RR/proximity as volatility rises.
    - Support both long and short futures signals.
    - Keep the first migration deterministic and easy to backtest.

    This is a V1 signal engine for dry-run and research. It is not a
    recommendation to enable live execution without validation.
    """

    INTERFACE_VERSION = 3
    can_short: bool = True

    timeframe = "15m"
    startup_candle_count: int = 240
    process_only_new_candles = True

    # Keep ROI effectively out of the way so structural exits drive research.
    minimal_roi = {"0": 10.0}
    stoploss = -0.08
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    TOP_SETUP_LIMIT = 10

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)

        dataframe["demand"] = dataframe["low"].rolling(20, min_periods=20).min().shift(1)
        dataframe["supply"] = dataframe["high"].rolling(20, min_periods=20).max().shift(1)

        dataframe["bull_structure"] = (
            (dataframe["ema20"] > dataframe["ema50"])
            & (dataframe["ema50"] > dataframe["ema200"])
        ).astype(int)
        dataframe["bear_structure"] = (
            (dataframe["ema20"] < dataframe["ema50"])
            & (dataframe["ema50"] < dataframe["ema200"])
        ).astype(int)
        return dataframe

    @informative("4h")
    def populate_indicators_4h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)

        dataframe["demand"] = dataframe["low"].rolling(24, min_periods=24).min().shift(1)
        dataframe["supply"] = dataframe["high"].rolling(24, min_periods=24).max().shift(1)

        dataframe["bull_structure"] = (
            (dataframe["ema20"] > dataframe["ema50"])
            & (dataframe["ema50"] > dataframe["ema200"])
        ).astype(int)
        dataframe["bear_structure"] = (
            (dataframe["ema20"] < dataframe["ema50"])
            & (dataframe["ema50"] < dataframe["ema200"])
        ).astype(int)
        return dataframe

    @staticmethod
    def _rr_score(rr):
        return np.select(
            [rr >= 3.0, rr >= 2.5, rr >= 2.0],
            [15, 12, 8],
            default=0,
        )

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        dataframe["volume_mean20"] = dataframe["volume"].rolling(20, min_periods=20).mean()
        dataframe["volume_ratio"] = dataframe["volume"] / dataframe["volume_mean20"].replace(0, np.nan)

        dataframe["prior_high20"] = dataframe["high"].rolling(20, min_periods=20).max().shift(1)
        dataframe["prior_low20"] = dataframe["low"].rolling(20, min_periods=20).min().shift(1)

        day_high = dataframe["high"].rolling(96, min_periods=96).max()
        day_low = dataframe["low"].rolling(96, min_periods=96).min()
        dataframe["range_24h"] = (day_high / day_low.replace(0, np.nan)) - 1.0

        # 0 NORMAL, 1 HIGH, 2 VERY_HIGH, 3 EXTREME
        dataframe["vol_regime"] = np.select(
            [
                dataframe["range_24h"] >= 0.25,
                dataframe["range_24h"] >= 0.15,
                dataframe["range_24h"] >= 0.08,
            ],
            [3, 2, 1],
            default=0,
        )

        dataframe["max_entry_distance_atr"] = np.select(
            [
                dataframe["vol_regime"] == 3,
                dataframe["vol_regime"] == 2,
                dataframe["vol_regime"] == 1,
            ],
            [0.30, 0.45, 0.60],
            default=0.75,
        )

        dataframe["required_score"] = np.select(
            [
                dataframe["vol_regime"] == 3,
                dataframe["vol_regime"] == 2,
                dataframe["vol_regime"] == 1,
            ],
            [90, 85, 82],
            default=80,
        )

        dataframe["required_rr"] = np.select(
            [
                dataframe["vol_regime"] == 3,
                dataframe["vol_regime"] == 2,
                dataframe["vol_regime"] == 1,
            ],
            [3.0, 2.5, 2.2],
            default=2.0,
        )

        dataframe["sweep_long"] = (
            (dataframe["low"] < dataframe["prior_low20"])
            & (dataframe["close"] > dataframe["prior_low20"])
        )
        dataframe["sweep_short"] = (
            (dataframe["high"] > dataframe["prior_high20"])
            & (dataframe["close"] < dataframe["prior_high20"])
        )

        dataframe["breakout_long"] = dataframe["close"] > dataframe["prior_high20"]
        dataframe["breakout_short"] = dataframe["close"] < dataframe["prior_low20"]

        dataframe["recent_breakout_long"] = (
            dataframe["breakout_long"].shift(1).rolling(4, min_periods=1).max().fillna(0) > 0
        )
        dataframe["recent_breakout_short"] = (
            dataframe["breakout_short"].shift(1).rolling(4, min_periods=1).max().fillna(0) > 0
        )

        dataframe["reclaim_long"] = (
            (dataframe["close"] > dataframe["ema20"])
            & (dataframe["close"].shift(1) <= dataframe["ema20"].shift(1))
        )
        dataframe["reclaim_short"] = (
            (dataframe["close"] < dataframe["ema20"])
            & (dataframe["close"].shift(1) >= dataframe["ema20"].shift(1))
        )

        atr_safe = dataframe["atr"].replace(0, np.nan)
        dataframe["long_entry_distance_atr"] = (
            (dataframe["close"] - dataframe["demand_1h"]).abs() / atr_safe
        )
        dataframe["short_entry_distance_atr"] = (
            (dataframe["supply_1h"] - dataframe["close"]).abs() / atr_safe
        )

        # Structural invalidation and target.
        dataframe["long_stop"] = dataframe["demand_1h"] - (0.20 * dataframe["atr_1h"])
        dataframe["short_stop"] = dataframe["supply_1h"] + (0.20 * dataframe["atr_1h"])

        # Prefer the nearest valid 1H structural target; otherwise fall back to 4H.
        dataframe["long_target"] = np.where(
            dataframe["supply_1h"] > dataframe["close"],
            dataframe["supply_1h"],
            dataframe["supply_4h"],
        )
        dataframe["short_target"] = np.where(
            dataframe["demand_1h"] < dataframe["close"],
            dataframe["demand_1h"],
            dataframe["demand_4h"],
        )

        long_risk = dataframe["close"] - dataframe["long_stop"]
        short_risk = dataframe["short_stop"] - dataframe["close"]

        dataframe["long_rr"] = np.where(
            (long_risk > 0) & (dataframe["long_target"] > dataframe["close"]),
            (dataframe["long_target"] - dataframe["close"]) / long_risk,
            np.nan,
        )
        dataframe["short_rr"] = np.where(
            (short_risk > 0) & (dataframe["short_target"] < dataframe["close"]),
            (dataframe["close"] - dataframe["short_target"]) / short_risk,
            np.nan,
        )

        long_location = (
            dataframe["long_entry_distance_atr"] <= dataframe["max_entry_distance_atr"]
        )
        short_location = (
            dataframe["short_entry_distance_atr"] <= dataframe["max_entry_distance_atr"]
        )

        long_confirmation = dataframe["sweep_long"] | dataframe["reclaim_long"]
        short_confirmation = dataframe["sweep_short"] | dataframe["reclaim_short"]

        long_breakout_retest = (
            dataframe["recent_breakout_long"]
            & (
                (dataframe["close"] - dataframe["prior_high20"]).abs()
                / atr_safe
                <= 0.50
            )
            & (dataframe["close"] >= dataframe["prior_high20"])
        )
        short_breakout_retest = (
            dataframe["recent_breakout_short"]
            & (
                (dataframe["close"] - dataframe["prior_low20"]).abs()
                / atr_safe
                <= 0.50
            )
            & (dataframe["close"] <= dataframe["prior_low20"])
        )

        long_trend_pullback = (
            (dataframe["bull_structure_4h"] == 1)
            & (dataframe["bull_structure_1h"] == 1)
            & long_location
            & dataframe["reclaim_long"]
        )
        short_trend_pullback = (
            (dataframe["bear_structure_4h"] == 1)
            & (dataframe["bear_structure_1h"] == 1)
            & short_location
            & dataframe["reclaim_short"]
        )

        long_reversal = (
            dataframe["sweep_long"]
            & (dataframe["rsi"] < 48)
            & (dataframe["volume_ratio"] >= 1.0)
        )
        short_reversal = (
            dataframe["sweep_short"]
            & (dataframe["rsi"] > 52)
            & (dataframe["volume_ratio"] >= 1.0)
        )

        long_high_vol = (
            (dataframe["vol_regime"] >= 1)
            & (dataframe["bull_structure_1h"] == 1)
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["volume_ratio"] >= 1.40)
            & long_location
        )
        short_high_vol = (
            (dataframe["vol_regime"] >= 1)
            & (dataframe["bear_structure_1h"] == 1)
            & (dataframe["close"] < dataframe["ema20"])
            & (dataframe["volume_ratio"] >= 1.40)
            & short_location
        )

        dataframe["long_setup"] = (
            long_trend_pullback
            | long_breakout_retest
            | long_reversal
            | long_high_vol
        )
        dataframe["short_setup"] = (
            short_trend_pullback
            | short_breakout_retest
            | short_reversal
            | short_high_vol
        )

        dataframe["long_setup_name"] = np.select(
            [
                long_trend_pullback,
                long_breakout_retest,
                long_reversal,
                long_high_vol,
            ],
            [
                "trend_pullback",
                "breakout_retest",
                "sweep_reversal",
                "high_vol_continuation",
            ],
            default="none",
        )
        dataframe["short_setup_name"] = np.select(
            [
                short_trend_pullback,
                short_breakout_retest,
                short_reversal,
                short_high_vol,
            ],
            [
                "trend_pullback",
                "breakout_retest",
                "sweep_reversal",
                "high_vol_continuation",
            ],
            default="none",
        )

        dataframe["long_score"] = (
            20 * (dataframe["bull_structure_4h"] == 1).astype(int)
            + 10 * (dataframe["bull_structure_1h"] == 1).astype(int)
            + 20 * long_location.astype(int)
            + 20 * (long_confirmation | long_breakout_retest).astype(int)
            + 10 * (dataframe["volume_ratio"] >= 1.20).astype(int)
            + self._rr_score(dataframe["long_rr"])
            + 5 * (dataframe["long_entry_distance_atr"] <= 0.35).astype(int)
        )

        dataframe["short_score"] = (
            20 * (dataframe["bear_structure_4h"] == 1).astype(int)
            + 10 * (dataframe["bear_structure_1h"] == 1).astype(int)
            + 20 * short_location.astype(int)
            + 20 * (short_confirmation | short_breakout_retest).astype(int)
            + 10 * (dataframe["volume_ratio"] >= 1.20).astype(int)
            + self._rr_score(dataframe["short_rr"])
            + 5 * (dataframe["short_entry_distance_atr"] <= 0.35).astype(int)
        )

        dataframe["ready_long"] = (
            dataframe["long_setup"]
            & (dataframe["long_score"] >= dataframe["required_score"])
            & (dataframe["long_rr"] >= dataframe["required_rr"])
            & long_location
        )

        dataframe["ready_short"] = (
            dataframe["short_setup"]
            & (dataframe["short_score"] >= dataframe["required_score"])
            & (dataframe["short_rr"] >= dataframe["required_rr"])
            & short_location
        )

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_mask = dataframe["ready_long"] & (dataframe["volume"] > 0)
        short_mask = dataframe["ready_short"] & (dataframe["volume"] > 0)

        dataframe.loc[long_mask, "enter_long"] = 1
        dataframe.loc[short_mask, "enter_short"] = 1

        dataframe.loc[long_mask, "enter_tag"] = (
            "LONG_"
            + dataframe.loc[long_mask, "long_setup_name"].astype(str)
            + "_S"
            + dataframe.loc[long_mask, "long_score"].astype(int).astype(str)
        )
        dataframe.loc[short_mask, "enter_tag"] = (
            "SHORT_"
            + dataframe.loc[short_mask, "short_setup_name"].astype(str)
            + "_S"
            + dataframe.loc[short_mask, "short_score"].astype(int).astype(str)
        )
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["close"] >= dataframe["long_target"])
                | (
                    (dataframe["bear_structure_1h"] == 1)
                    & (dataframe["close"] < dataframe["ema20"])
                )
            )
            & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1

        dataframe.loc[
            (
                (dataframe["close"] <= dataframe["short_target"])
                | (
                    (dataframe["bull_structure_1h"] == 1)
                    & (dataframe["close"] > dataframe["ema20"])
                )
            )
            & (dataframe["volume"] > 0),
            "exit_short",
        ] = 1
        return dataframe

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> bool:
        """
        Keep the framework safe if analyzed data is unavailable.
        Score/RR gates are already encoded into enter_long/enter_short.

        A cross-pair top-N ranking layer can be added after the dry-run data
        confirms the score distribution across the full universe.
        """
        return True

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        # Research phase: stay at 1x even though futures are enabled.
        return 1.0
