import pandas as pd

MAX_CORR = 0.82
MAX_PER_CLUSTER = 2
LOOKBACK = 96


def _returns(frame):
    if frame is None or frame.empty:
        return None
    return frame["close"].astype(float).pct_change().dropna().tail(LOOKBACK)


def suppress_correlated(results, frames_by_symbol):
    kept = []

    for item in results:
        item["correlation_suppressed"] = False
        if item.get("bucket") not in (
            "MOMENTUM_READY",
            "PULLBACK_READY",
            "HIGH_VOL_DEVELOPING",
        ):
            continue

        candidate = _returns(
            (frames_by_symbol.get(item.get("symbol")) or {}).get("15M")
        )
        matches = []

        if candidate is not None:
            for leader in kept:
                if leader.get("direction") != item.get("direction"):
                    continue

                other = _returns(
                    (frames_by_symbol.get(leader.get("symbol")) or {}).get("15M")
                )
                if other is None:
                    continue

                joined = pd.concat([candidate, other], axis=1).dropna()
                if len(joined) < 30:
                    continue

                corr = joined.iloc[:, 0].corr(joined.iloc[:, 1])
                if pd.notna(corr) and abs(float(corr)) >= MAX_CORR:
                    matches.append((leader, float(corr)))

        if len(matches) >= MAX_PER_CLUSTER:
            item["correlation_suppressed"] = True
            item["correlation_reason"] = ", ".join(
                f"{leader.get('symbol')}({corr:+.2f})"
                for leader, corr in matches[:2]
            )
        else:
            kept.append(item)

    return results
