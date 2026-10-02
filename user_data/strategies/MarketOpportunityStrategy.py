# pragma pylint: disable=missing-docstring, invalid-name
from __future__ import annotations

from datetime import datetime
from typing import Optional

import numpy as np
from pandas import DataFrame

import talib.abstract as ta

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative, stoploss_from_absolute


class MarketOpportunityStrategy(IStrategy):
    """
    Broad-market Gate USDT perpetual crypto opportunity scanner.

    Design goals:
    - High volatility is a regime, not a prerequisite.
    - Define structure/location first, then compute RR.
    - Require stronger score/RR/proximity as volatility rises.
    - Support both long and short futures signals.
    - Keep the first migration deterministic and easy to backtest.

    V3.5 keeps the structural risk model but moves fatal quality defects out
    of the additive score. Opposing 4H structure, a nearby 15m obstacle, and
    volatility contraction can now veto READY even when the raw score is high.
    DEVELOPING remains watchlist-only research telemetry.
    """

    INTERFACE_VERSION = 3
    can_short: bool = True

    timeframe = "15m"
    startup_candle_count: int = 240
    process_only_new_candles = True

    # Keep ROI effectively out of the way so structural exits drive research.
    minimal_roi = {"0": 10.0}
    # Wide emergency floor. custom_stoploss() tightens this to structural invalidation.
    stoploss = -0.20
    use_custom_stoploss = True
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
        dataframe["atr_baseline96"] = dataframe["atr"].rolling(96, min_periods=48).median()
        dataframe["atr_expansion"] = (
            dataframe["atr"] / dataframe["atr_baseline96"].replace(0, np.nan)
        )

        dataframe["volume_mean20"] = dataframe["volume"].rolling(20, min_periods=20).mean()
        dataframe["volume_ratio"] = dataframe["volume"] / dataframe["volume_mean20"].replace(0, np.nan)

        dataframe["prior_high20"] = dataframe["high"].rolling(20, min_periods=20).max().shift(1)
        dataframe["prior_low20"] = dataframe["low"].rolling(20, min_periods=20).min().shift(1)
        dataframe["micro_high5"] = dataframe["high"].rolling(5, min_periods=5).max().shift(1)
        dataframe["micro_low5"] = dataframe["low"].rolling(5, min_periods=5).min().shift(1)

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

        dataframe["ema_touch_long"] = (
            (dataframe["low"] <= dataframe["ema20"])
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["close"] > dataframe["open"])
        )
        dataframe["ema_touch_short"] = (
            (dataframe["high"] >= dataframe["ema20"])
            & (dataframe["close"] < dataframe["ema20"])
            & (dataframe["close"] < dataframe["open"])
        )

        dataframe["recent_sweep_long"] = (
            dataframe["sweep_long"].rolling(4, min_periods=1).max().fillna(0) > 0
        )
        dataframe["recent_sweep_short"] = (
            dataframe["sweep_short"].rolling(4, min_periods=1).max().fillna(0) > 0
        )
        dataframe["choch_long"] = (
            dataframe["recent_sweep_long"]
            & (dataframe["close"] > dataframe["micro_high5"])
            & (dataframe["close"] > dataframe["ema20"])
        )
        dataframe["choch_short"] = (
            dataframe["recent_sweep_short"]
            & (dataframe["close"] < dataframe["micro_low5"])
            & (dataframe["close"] < dataframe["ema20"])
        )

        atr_safe = dataframe["atr"].replace(0, np.nan)
        dataframe["long_entry_distance_atr"] = (
            (dataframe["close"] - dataframe["demand_1h"]).abs() / atr_safe
        )
        dataframe["short_entry_distance_atr"] = (
            (dataframe["supply_1h"] - dataframe["close"]).abs() / atr_safe
        )
        dataframe["ema20_1h_distance_atr"] = (
            (dataframe["close"] - dataframe["ema20_1h"]).abs() / atr_safe
        )
        dataframe["long_pullback_distance_atr"] = np.minimum(
            dataframe["long_entry_distance_atr"],
            dataframe["ema20_1h_distance_atr"],
        )
        dataframe["short_pullback_distance_atr"] = np.minimum(
            dataframe["short_entry_distance_atr"],
            dataframe["ema20_1h_distance_atr"],
        )

        # Structural invalidation with a minimum noise buffer of 0.75 x 15m ATR.
        long_zone_stop = dataframe["demand_1h"] - (0.20 * dataframe["atr_1h"])
        short_zone_stop = dataframe["supply_1h"] + (0.20 * dataframe["atr_1h"])
        dataframe["long_stop"] = np.minimum(
            long_zone_stop,
            dataframe["close"] - (0.75 * dataframe["atr"]),
        )
        dataframe["short_stop"] = np.maximum(
            short_zone_stop,
            dataframe["close"] + (0.75 * dataframe["atr"]),
        )

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

        # V3.5 path-quality gate: the nearest 15m swing level between entry and
        # the structural target is treated as an obstacle. READY needs at least
        # 0.75R of clean space before that obstacle (or the target itself).
        long_local_obstacle = np.where(
            (dataframe["prior_high20"] > dataframe["close"])
            & (dataframe["prior_high20"] < dataframe["long_target"]),
            dataframe["prior_high20"],
            dataframe["long_target"],
        )
        short_local_obstacle = np.where(
            (dataframe["prior_low20"] < dataframe["close"])
            & (dataframe["prior_low20"] > dataframe["short_target"]),
            dataframe["prior_low20"],
            dataframe["short_target"],
        )
        dataframe["long_obstacle_clearance_r"] = np.where(
            long_risk > 0,
            (long_local_obstacle - dataframe["close"]) / long_risk,
            np.nan,
        )
        dataframe["short_obstacle_clearance_r"] = np.where(
            short_risk > 0,
            (dataframe["close"] - short_local_obstacle) / short_risk,
            np.nan,
        )

        long_location = (
            dataframe["long_entry_distance_atr"] <= dataframe["max_entry_distance_atr"]
        )
        short_location = (
            dataframe["short_entry_distance_atr"] <= dataframe["max_entry_distance_atr"]
        )
        long_pullback_location = (
            dataframe["long_pullback_distance_atr"] <= dataframe["max_entry_distance_atr"]
        )
        short_pullback_location = (
            dataframe["short_pullback_distance_atr"] <= dataframe["max_entry_distance_atr"]
        )

        # A raw liquidity sweep is not enough. Require either EMA reclaim/touch,
        # a micro-structure CHoCH, or a confirmed breakout/retest path.
        long_confirmation = (
            dataframe["reclaim_long"] | dataframe["ema_touch_long"] | dataframe["choch_long"]
        )
        short_confirmation = (
            dataframe["reclaim_short"] | dataframe["ema_touch_short"] | dataframe["choch_short"]
        )

        long_breakout_retest = (
            dataframe["recent_breakout_long"]
            & (dataframe["bull_structure_1h"] == 1)
            & (dataframe["bear_structure_4h"] == 0)
            & (
                (dataframe["close"] - dataframe["prior_high20"]).abs()
                / atr_safe
                <= 0.55
            )
            & (dataframe["close"] >= dataframe["prior_high20"])
            & (dataframe["volume_ratio"] >= 1.10)
        )
        short_breakout_retest = (
            dataframe["recent_breakout_short"]
            & (dataframe["bear_structure_1h"] == 1)
            & (dataframe["bull_structure_4h"] == 0)
            & (
                (dataframe["close"] - dataframe["prior_low20"]).abs()
                / atr_safe
                <= 0.55
            )
            & (dataframe["close"] <= dataframe["prior_low20"])
            & (dataframe["volume_ratio"] >= 1.10)
        )

        long_trend_pullback = (
            (dataframe["bull_structure_4h"] == 1)
            & (dataframe["bull_structure_1h"] == 1)
            & long_pullback_location
            & (dataframe["reclaim_long"] | dataframe["ema_touch_long"])
        )
        short_trend_pullback = (
            (dataframe["bear_structure_4h"] == 1)
            & (dataframe["bear_structure_1h"] == 1)
            & short_pullback_location
            & (dataframe["reclaim_short"] | dataframe["ema_touch_short"])
        )

        # Reversal requires liquidity sweep -> micro CHoCH and cannot fight a
        # strongly aligned 4H trend. This removes the weak naked-sweep entries
        # seen in the V1 smoke backtest.
        long_reversal = (
            dataframe["choch_long"]
            & (dataframe["bear_structure_4h"] == 0)
            & long_location
            & (dataframe["volume_ratio"] >= 1.20)
            & (dataframe["rsi"] <= 58)
        )
        short_reversal = (
            dataframe["choch_short"]
            & (dataframe["bull_structure_4h"] == 0)
            & short_location
            & (dataframe["volume_ratio"] >= 1.25)
            & (dataframe["rsi"] >= 42)
        )

        long_high_vol = (
            (dataframe["vol_regime"] >= 1)
            & (dataframe["bull_structure_1h"] == 1)
            & (dataframe["bear_structure_4h"] == 0)
            & (dataframe["atr_expansion"] >= 1.05)
            & dataframe["recent_breakout_long"]
            & (dataframe["close"] > dataframe["ema20"])
            & (dataframe["volume_ratio"] >= 1.40)
            & (
                (dataframe["close"] - dataframe["prior_high20"]).abs()
                / atr_safe
                <= 0.70
            )
        )
        short_high_vol = (
            (dataframe["vol_regime"] >= 1)
            & (dataframe["bear_structure_1h"] == 1)
            & (dataframe["bull_structure_4h"] == 0)
            & (dataframe["atr_expansion"] >= 1.05)
            & dataframe["recent_breakout_short"]
            & (dataframe["close"] < dataframe["ema20"])
            & (dataframe["volume_ratio"] >= 1.40)
            & (
                (dataframe["close"] - dataframe["prior_low20"]).abs()
                / atr_safe
                <= 0.70
            )
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

        long_location_quality = long_location | long_pullback_location | long_breakout_retest
        short_location_quality = short_location | short_pullback_location | short_breakout_retest

        # V3.5 calibration: opposing 4H structure remains a fatal defect for
        # breakout/continuation paths. Obstacle clearance and generic ATR
        # expansion stay as telemetry until outcome data proves a useful cutoff.
        long_mtf_hard_gate = dataframe["bear_structure_4h"] == 0
        short_mtf_hard_gate = dataframe["bull_structure_4h"] == 0

        dataframe["long_score"] = (
            20 * (dataframe["bull_structure_4h"] == 1).astype(int)
            + 10 * (dataframe["bull_structure_1h"] == 1).astype(int)
            + 20 * long_location_quality.astype(int)
            + 20 * (long_confirmation | long_breakout_retest).astype(int)
            + 10 * (dataframe["volume_ratio"] >= 1.20).astype(int)
            + self._rr_score(dataframe["long_rr"])
            + 5 * (
                np.minimum(
                    dataframe["long_entry_distance_atr"],
                    dataframe["long_pullback_distance_atr"],
                )
                <= 0.35
            ).astype(int)
        )

        dataframe["short_score"] = (
            20 * (dataframe["bear_structure_4h"] == 1).astype(int)
            + 10 * (dataframe["bear_structure_1h"] == 1).astype(int)
            + 20 * short_location_quality.astype(int)
            + 20 * (short_confirmation | short_breakout_retest).astype(int)
            + 10 * (dataframe["volume_ratio"] >= 1.20).astype(int)
            + self._rr_score(dataframe["short_rr"])
            + 5 * (
                np.minimum(
                    dataframe["short_entry_distance_atr"],
                    dataframe["short_pullback_distance_atr"],
                )
                <= 0.35
            ).astype(int)
        )

        # Reversals have stricter gates because V1 showed low-quality sweep
        # entries, especially shorts. Other setup types keep regime-based gates.
        dataframe["long_required_score"] = np.maximum(
            dataframe["required_score"],
            np.where(dataframe["long_setup_name"] == "sweep_reversal", 90, dataframe["required_score"]),
        )
        dataframe["short_required_score"] = np.maximum(
            dataframe["required_score"],
            np.where(dataframe["short_setup_name"] == "sweep_reversal", 92, dataframe["required_score"]),
        )
        dataframe["long_required_rr"] = np.maximum(
            dataframe["required_rr"],
            np.where(dataframe["long_setup_name"] == "sweep_reversal", 2.5, dataframe["required_rr"]),
        )
        dataframe["short_required_rr"] = np.maximum(
            dataframe["required_rr"],
            np.where(dataframe["short_setup_name"] == "sweep_reversal", 2.8, dataframe["required_rr"]),
        )

        dataframe["ready_long"] = (
            dataframe["long_setup"]
            & (dataframe["long_score"] >= dataframe["long_required_score"])
            & (dataframe["long_rr"] >= dataframe["long_required_rr"])
            & long_location_quality
            & long_mtf_hard_gate
        )

        dataframe["ready_short"] = (
            dataframe["short_setup"]
            & (dataframe["short_score"] >= dataframe["short_required_score"])
            & (dataframe["short_rr"] >= dataframe["short_required_rr"])
            & short_location_quality
            & short_mtf_hard_gate
        )

        # Telemetry for the next calibration phase. These columns do not change
        # backtest entries; they prepare DEVELOPING states and cross-pair ranking.
        dataframe["long_rank_distance_atr"] = np.minimum(
            dataframe["long_entry_distance_atr"],
            dataframe["long_pullback_distance_atr"],
        )
        dataframe["short_rank_distance_atr"] = np.minimum(
            dataframe["short_entry_distance_atr"],
            dataframe["short_pullback_distance_atr"],
        )
        dataframe["long_opportunity_value"] = (
            dataframe["long_score"]
            + (np.clip(dataframe["long_rr"], 0, 5) * 4)
            - (np.clip(dataframe["long_rank_distance_atr"], 0, 2) * 5)
        )
        dataframe["short_opportunity_value"] = (
            dataframe["short_score"]
            + (np.clip(dataframe["short_rr"], 0, 5) * 4)
            - (np.clip(dataframe["short_rank_distance_atr"], 0, 2) * 5)
        )

        dataframe["developing_long"] = (
            dataframe["long_setup"]
            & ~dataframe["ready_long"]
            & (dataframe["long_score"] >= (dataframe["long_required_score"] - 8))
            & (dataframe["long_rr"] >= (dataframe["long_required_rr"] * 0.80))
            & long_location_quality
            & long_mtf_hard_gate
        )
        dataframe["developing_short"] = (
            dataframe["short_setup"]
            & ~dataframe["ready_short"]
            & (dataframe["short_score"] >= (dataframe["short_required_score"] - 8))
            & (dataframe["short_rr"] >= (dataframe["short_required_rr"] * 0.80))
            & short_location_quality
            & short_mtf_hard_gate
        )

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_mask = dataframe["ready_long"] & (dataframe["volume"] > 0)
        short_mask = dataframe["ready_short"] & (dataframe["volume"] > 0)

        dataframe.loc[long_mask, "enter_long"] = 1
        dataframe.loc[short_mask, "enter_short"] = 1

        # Encode score, volatility regime and RR into the tag so research
        # results can be segmented without changing entry rules.
        dataframe.loc[long_mask, "enter_tag"] = (
            "LONG_"
            + dataframe.loc[long_mask, "long_setup_name"].astype(str)
            + "_S"
            + dataframe.loc[long_mask, "long_score"].astype(int).astype(str)
            + "_V"
            + dataframe.loc[long_mask, "vol_regime"].astype(int).astype(str)
            + "_R"
            + (dataframe.loc[long_mask, "long_rr"] * 10).round().astype(int).astype(str)
            + "_A"
            + (dataframe.loc[long_mask, "atr_expansion"].fillna(0) * 100).round().astype(int).astype(str)
            + "_O"
            + (dataframe.loc[long_mask, "long_obstacle_clearance_r"].fillna(0).clip(0, 9.9) * 10).round().astype(int).astype(str)
            + "_Q"
            + dataframe.loc[long_mask, "rsi"].fillna(0).round().astype(int).astype(str)
            + "_D"
            + (dataframe.loc[long_mask, "long_rank_distance_atr"].fillna(0).clip(0, 9.9) * 10).round().astype(int).astype(str)
        )
        dataframe.loc[short_mask, "enter_tag"] = (
            "SHORT_"
            + dataframe.loc[short_mask, "short_setup_name"].astype(str)
            + "_S"
            + dataframe.loc[short_mask, "short_score"].astype(int).astype(str)
            + "_V"
            + dataframe.loc[short_mask, "vol_regime"].astype(int).astype(str)
            + "_R"
            + (dataframe.loc[short_mask, "short_rr"] * 10).round().astype(int).astype(str)
            + "_A"
            + (dataframe.loc[short_mask, "atr_expansion"].fillna(0) * 100).round().astype(int).astype(str)
            + "_O"
            + (dataframe.loc[short_mask, "short_obstacle_clearance_r"].fillna(0).clip(0, 9.9) * 10).round().astype(int).astype(str)
            + "_Q"
            + dataframe.loc[short_mask, "rsi"].fillna(0).round().astype(int).astype(str)
            + "_D"
            + (dataframe.loc[short_mask, "short_rank_distance_atr"].fillna(0).clip(0, 9.9) * 10).round().astype(int).astype(str)
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

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        """
        Use the same structural invalidation model that is used to calculate RR.
        Freqtrade will only tighten a custom stop during a trade, so a later
        structural level cannot silently increase risk.
        """
        if not self.dp:
            return None
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return None

        candle = dataframe.iloc[-1].squeeze()
        key = "short_stop" if trade.is_short else "long_stop"
        stop_rate = candle.get(key)
        if stop_rate is None or not np.isfinite(stop_rate):
            return None
        stop_rate = float(stop_rate)

        if (not trade.is_short and stop_rate >= current_rate) or (
            trade.is_short and stop_rate <= current_rate
        ):
            return None

        return stoploss_from_absolute(
            stop_rate,
            current_rate=current_rate,
            is_short=trade.is_short,
            leverage=trade.leverage,
        )

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
