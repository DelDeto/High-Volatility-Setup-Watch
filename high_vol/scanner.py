from smc_analysis import (
    analyze_smc,
    derive_mtf_alignment,
    derive_overall_status,
)
from trade_plan import build_trade_plan

from .ranker import score_high_vol_setup


def analyze_symbol(symbol, frames, ticker):
    analysis_4h = analyze_smc(frames["4H"], timeframe="4H")
    analysis_1h = analyze_smc(frames["1H"], timeframe="1H")
    analysis_15m = analyze_smc(frames["15M"], timeframe="15M")

    status = derive_overall_status(
        analysis_4h,
        analysis_1h,
        analysis_15m,
    )

    alignment = derive_mtf_alignment(
        analysis_4h,
        analysis_1h,
        analysis_15m,
    )

    plan = build_trade_plan(
        analysis_4h,
        analysis_1h,
        analysis_15m,
        status,
        mtf_alignment=alignment,
    )

    ranked = score_high_vol_setup(
        analysis_4h,
        analysis_1h,
        analysis_15m,
        alignment,
        plan,
        ticker,
        frames["15M"],
    )

    direction = plan.get("direction")
    if not direction:
        setup_direction = (analysis_15m.get("setup") or {}).get("direction")
        if setup_direction == "bullish":
            direction = "long"
        elif setup_direction == "bearish":
            direction = "short"

    return {
        "symbol": symbol,
        "status": status,
        "direction": direction,
        "score": ranked["score"],
        "bucket": ranked["bucket"],
        "entry_distance_atr": ranked["entry_distance_atr"],
        "score_breakdown": ranked["score_breakdown"],
        "volatility": ranked["volatility"],
        "mtf_alignment": alignment.get("label", "NEUTRAL"),
        "regime_4h": analysis_4h.get("regime"),
        "regime_1h": analysis_1h.get("regime"),
        "regime_15m": analysis_15m.get("regime"),
        "ticker": ticker or {},
        "trade_plan": plan,
        "analysis_4h": analysis_4h,
        "analysis_1h": analysis_1h,
        "analysis_15m": analysis_15m,
    }
