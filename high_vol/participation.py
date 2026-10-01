def apply_participation_context(tickers, previous_hold_vol=None):
    previous_hold_vol = previous_hold_vol or {}
    snapshot = {}

    for symbol, ticker in tickers.items():
        current = ticker.get("hold_vol")
        previous = previous_hold_vol.get(symbol)
        change_pct = None
        regime = "N/A"

        if current is not None:
            snapshot[symbol] = float(current)

        if current is not None and previous not in (None, 0):
            change_pct = (
                (float(current) - float(previous))
                / abs(float(previous))
                * 100.0
            )
            price_change = ticker.get("change_rate_24h")

            if change_pct >= 0.15:
                if price_change is not None and float(price_change) > 0:
                    regime = "LONG_BUILD"
                elif price_change is not None and float(price_change) < 0:
                    regime = "SHORT_BUILD"
                else:
                    regime = "OI_EXPANDING"
            elif change_pct <= -0.15:
                regime = "DELEVERAGING"
            else:
                regime = "STABLE"

        ticker["hold_vol_change_pct"] = change_pct
        ticker["participation_context"] = {
            "regime": regime,
            "hold_vol_change_pct": change_pct,
        }

    return snapshot
