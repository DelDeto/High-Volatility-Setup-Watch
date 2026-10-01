import numpy as np
import pandas as pd


def _atr(df, period=14):
    prev_close = df["close"].shift(1)

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.rolling(
        period,
        min_periods=1,
    ).mean()


def _swing_masks(
    df,
    window=2,
):
    high_mask = pd.Series(
        True,
        index=df.index,
    )

    low_mask = pd.Series(
        True,
        index=df.index,
    )

    for offset in range(
        1,
        window + 1,
    ):
        high_mask &= (
            df["high"]
            > df["high"].shift(offset)
        )

        high_mask &= (
            df["high"]
            > df["high"].shift(-offset)
        )

        low_mask &= (
            df["low"]
            < df["low"].shift(offset)
        )

        low_mask &= (
            df["low"]
            < df["low"].shift(-offset)
        )

    return (
        high_mask.fillna(False),
        low_mask.fillna(False),
    )


def _infer_trend(
    swing_highs,
    swing_lows,
):
    """
    HH + HL -> bullish
    LH + LL -> bearish
    otherwise -> mixed
    """

    if (
        len(swing_highs) >= 2
        and len(swing_lows) >= 2
    ):
        prev_high = swing_highs[-2][1]
        last_high = swing_highs[-1][1]

        prev_low = swing_lows[-2][1]
        last_low = swing_lows[-1][1]

        if (
            last_high > prev_high
            and last_low > prev_low
        ):
            return "bullish"

        if (
            last_high < prev_high
            and last_low < prev_low
        ):
            return "bearish"

        return "mixed"

    return "neutral"


def _structure_events(
    df,
    high_mask,
    low_mask,
    window=2,
):
    """
    Rule-based BOS / CHoCH:
    - Close breaks a confirmed swing level.
    - Break in active direction = BOS.
    - Break opposite active direction = CHoCH.
    """

    events = []

    last_high = None
    last_low = None

    broken_highs = set()
    broken_lows = set()

    bias = "neutral"

    for i in range(len(df)):
        confirmed_index = (
            i - window
        )

        if confirmed_index >= 0:
            if bool(
                high_mask.iloc[
                    confirmed_index
                ]
            ):
                last_high = (
                    confirmed_index,
                    float(
                        df["high"].iloc[
                            confirmed_index
                        ]
                    ),
                )

            if bool(
                low_mask.iloc[
                    confirmed_index
                ]
            ):
                last_low = (
                    confirmed_index,
                    float(
                        df["low"].iloc[
                            confirmed_index
                        ]
                    ),
                )

        if i == 0:
            continue

        close_now = float(
            df["close"].iloc[i]
        )

        close_prev = float(
            df["close"].iloc[i - 1]
        )

        if last_high is not None:
            swing_index, level = (
                last_high
            )

            if (
                swing_index
                not in broken_highs
                and i > swing_index
                and close_now > level
                and close_prev <= level
            ):
                kind = (
                    "BOS"
                    if bias
                    in (
                        "neutral",
                        "bullish",
                    )
                    else "CHoCH"
                )

                events.append(
                    {
                        "index": i,
                        "time": (
                            df.index[
                                i
                            ].isoformat()
                        ),
                        "direction": (
                            "bullish"
                        ),
                        "kind": kind,
                        "level": level,
                        "close": close_now,
                    }
                )

                broken_highs.add(
                    swing_index
                )

                bias = "bullish"

        if last_low is not None:
            swing_index, level = (
                last_low
            )

            if (
                swing_index
                not in broken_lows
                and i > swing_index
                and close_now < level
                and close_prev >= level
            ):
                kind = (
                    "BOS"
                    if bias
                    in (
                        "neutral",
                        "bearish",
                    )
                    else "CHoCH"
                )

                events.append(
                    {
                        "index": i,
                        "time": (
                            df.index[
                                i
                            ].isoformat()
                        ),
                        "direction": (
                            "bearish"
                        ),
                        "kind": kind,
                        "level": level,
                        "close": close_now,
                    }
                )

                broken_lows.add(
                    swing_index
                )

                bias = "bearish"

    return events


def _untouched_swings(
    df,
    swings,
    side,
):
    """
    Keep only liquidity pools that have not already been traded through
    after the swing was formed.
    """

    valid = []

    for point in swings:
        index, price, timestamp = (
            point
        )

        future = df.iloc[
            index + 1 :
        ]

        if future.empty:
            untouched = True

        elif side == "high":
            untouched = (
                float(
                    future[
                        "high"
                    ].max()
                )
                < price
            )

        else:
            untouched = (
                float(
                    future[
                        "low"
                    ].min()
                )
                > price
            )

        if untouched:
            valid.append(
                point
            )

    return valid


def _find_liquidity(
    df,
    current_price,
    swing_highs,
    swing_lows,
):
    """
    BSL / SSL are selected only from untouched confirmed swings.
    This prevents stale historical highs/lows that were already swept
    from being displayed as active liquidity.
    """

    active_highs = (
        _untouched_swings(
            df,
            swing_highs,
            "high",
        )
    )

    active_lows = (
        _untouched_swings(
            df,
            swing_lows,
            "low",
        )
    )

    overhead = [
        point
        for point in active_highs
        if point[1] > current_price
    ]

    below = [
        point
        for point in active_lows
        if point[1] < current_price
    ]

    bsl = (
        min(
            overhead,
            key=lambda item: item[1],
        )
        if overhead
        else None
    )

    ssl = (
        max(
            below,
            key=lambda item: item[1],
        )
        if below
        else None
    )

    def pack(point):
        if point is None:
            return None

        index, price, timestamp = (
            point
        )

        return {
            "index": index,
            "price": price,
            "time": timestamp,
        }

    return (
        pack(bsl),
        pack(ssl),
    )


def _find_fvgs(df):
    """
    Three-candle Fair Value Gap:
    bullish FVG: low[i] > high[i-2]
    bearish FVG: high[i] < low[i-2]
    """

    fvgs = []

    for i in range(
        2,
        len(df),
    ):
        high_two_back = float(
            df["high"].iloc[i - 2]
        )

        low_two_back = float(
            df["low"].iloc[i - 2]
        )

        high_now = float(
            df["high"].iloc[i]
        )

        low_now = float(
            df["low"].iloc[i]
        )

        if (
            low_now
            > high_two_back
        ):
            lower = high_two_back
            upper = low_now

            future = df.iloc[
                i + 1 :
            ]

            filled = (
                not future.empty
                and float(
                    future[
                        "low"
                    ].min()
                )
                <= lower
            )

            mitigated = (
                not future.empty
                and float(
                    future[
                        "low"
                    ].min()
                )
                < upper
            )

            fvgs.append(
                {
                    "type": (
                        "bullish"
                    ),
                    "index": i,
                    "time": (
                        df.index[
                            i
                        ].isoformat()
                    ),
                    "lower": lower,
                    "upper": upper,
                    "filled": bool(
                        filled
                    ),
                    "mitigated": bool(
                        mitigated
                    ),
                }
            )

        if (
            high_now
            < low_two_back
        ):
            lower = high_now
            upper = low_two_back

            future = df.iloc[
                i + 1 :
            ]

            filled = (
                not future.empty
                and float(
                    future[
                        "high"
                    ].max()
                )
                >= upper
            )

            mitigated = (
                not future.empty
                and float(
                    future[
                        "high"
                    ].max()
                )
                > lower
            )

            fvgs.append(
                {
                    "type": (
                        "bearish"
                    ),
                    "index": i,
                    "time": (
                        df.index[
                            i
                        ].isoformat()
                    ),
                    "lower": lower,
                    "upper": upper,
                    "filled": bool(
                        filled
                    ),
                    "mitigated": bool(
                        mitigated
                    ),
                }
            )

    return fvgs


def _find_displacements(
    df,
    atr,
):
    """
    Strong directional candle heuristic:
    body >= 1.15 ATR,
    body >= 65% of range,
    close near the directional extreme.
    """

    events = []

    body = (
        df["close"]
        - df["open"]
    ).abs()

    candle_range = (
        df["high"]
        - df["low"]
    ).replace(
        0,
        np.nan,
    )

    close_location = (
        (
            df["close"]
            - df["low"]
        )
        / candle_range
    )

    body_ratio = (
        body
        / candle_range
    )

    for i in range(
        1,
        len(df),
    ):
        atr_value = float(
            atr.iloc[i]
        )

        if (
            pd.isna(atr_value)
            or atr_value <= 0
        ):
            continue

        bullish = (
            df["close"].iloc[i]
            > df["open"].iloc[i]
            and float(
                body.iloc[i]
            )
            >= 1.15 * atr_value
            and float(
                body_ratio.iloc[i]
            )
            >= 0.65
            and float(
                close_location.iloc[i]
            )
            >= 0.75
        )

        bearish = (
            df["close"].iloc[i]
            < df["open"].iloc[i]
            and float(
                body.iloc[i]
            )
            >= 1.15 * atr_value
            and float(
                body_ratio.iloc[i]
            )
            >= 0.65
            and float(
                close_location.iloc[i]
            )
            <= 0.25
        )

        if bullish or bearish:
            events.append(
                {
                    "index": i,
                    "time": (
                        df.index[
                            i
                        ].isoformat()
                    ),
                    "direction": (
                        "bullish"
                        if bullish
                        else "bearish"
                    ),
                    "strength": float(
                        body.iloc[i]
                        / atr_value
                    ),
                    "open": float(
                        df["open"].iloc[i]
                    ),
                    "close": float(
                        df["close"].iloc[i]
                    ),
                    "high": float(
                        df["high"].iloc[i]
                    ),
                    "low": float(
                        df["low"].iloc[i]
                    ),
                }
            )

    return events


def _find_sweeps(
    df,
    high_mask,
    low_mask,
    window=2,
):
    """
    Liquidity Sweep heuristic:
    - BSL sweep: wick above confirmed swing high, close back below.
    - SSL sweep: wick below confirmed swing low, close back above.
    """

    sweeps = []

    last_high = None
    last_low = None

    swept_highs = set()
    swept_lows = set()

    for i in range(len(df)):
        confirmed_index = (
            i - window
        )

        if confirmed_index >= 0:
            if bool(
                high_mask.iloc[
                    confirmed_index
                ]
            ):
                last_high = (
                    confirmed_index,
                    float(
                        df["high"].iloc[
                            confirmed_index
                        ]
                    ),
                )

            if bool(
                low_mask.iloc[
                    confirmed_index
                ]
            ):
                last_low = (
                    confirmed_index,
                    float(
                        df["low"].iloc[
                            confirmed_index
                        ]
                    ),
                )

        high_now = float(
            df["high"].iloc[i]
        )

        low_now = float(
            df["low"].iloc[i]
        )

        close_now = float(
            df["close"].iloc[i]
        )

        if last_high:
            swing_index, level = (
                last_high
            )

            if (
                swing_index
                not in swept_highs
                and i > swing_index
                and high_now > level
                and close_now < level
            ):
                sweeps.append(
                    {
                        "index": i,
                        "time": (
                            df.index[
                                i
                            ].isoformat()
                        ),
                        "type": (
                            "BSL"
                        ),
                        "direction": (
                            "bearish"
                        ),
                        "level": level,
                        "extreme": high_now,
                    }
                )

                swept_highs.add(
                    swing_index
                )

        if last_low:
            swing_index, level = (
                last_low
            )

            if (
                swing_index
                not in swept_lows
                and i > swing_index
                and low_now < level
                and close_now > level
            ):
                sweeps.append(
                    {
                        "index": i,
                        "time": (
                            df.index[
                                i
                            ].isoformat()
                        ),
                        "type": (
                            "SSL"
                        ),
                        "direction": (
                            "bullish"
                        ),
                        "level": level,
                        "extreme": low_now,
                    }
                )

                swept_lows.add(
                    swing_index
                )

    return sweeps


def _find_retests(
    df,
    structure_events,
    atr,
    max_bars=8,
):
    """
    Retest of a broken structure level after BOS / CHoCH.
    """

    retests = []

    for event in structure_events:
        event_index = event[
            "index"
        ]

        level = float(
            event["level"]
        )

        end = min(
            len(df),
            event_index
            + max_bars
            + 1,
        )

        for i in range(
            event_index + 1,
            end,
        ):
            atr_value = float(
                atr.iloc[i]
            )

            tolerance = max(
                0.001,
                atr_value * 0.12,
            )

            low_now = float(
                df["low"].iloc[i]
            )

            high_now = float(
                df["high"].iloc[i]
            )

            close_now = float(
                df["close"].iloc[i]
            )

            if (
                event["direction"]
                == "bullish"
            ):
                touched = (
                    low_now
                    <= level + tolerance
                )

                held = (
                    close_now
                    >= level
                )

            else:
                touched = (
                    high_now
                    >= level - tolerance
                )

                held = (
                    close_now
                    <= level
                )

            if touched and held:
                retests.append(
                    {
                        "index": i,
                        "time": (
                            df.index[
                                i
                            ].isoformat()
                        ),
                        "direction": (
                            event[
                                "direction"
                            ]
                        ),
                        "structure_kind": (
                            event["kind"]
                        ),
                        "level": level,
                        "source_index": (
                            event_index
                        ),
                    }
                )
                break

    return retests


ZONE_POLICIES = {
    "15M": {
        "primary_age": 128,
        "max_age": 320,
        "max_distance_pct": 0.18,
        "max_mitigations": 3,
        "min_quality": 4.0,
    },
    "1H": {
        "primary_age": 168,
        "max_age": 360,
        "max_distance_pct": 0.30,
        "max_mitigations": 3,
        "min_quality": 4.0,
    },
    "4H": {
        "primary_age": 168,
        "max_age": 360,
        "max_distance_pct": 0.45,
        "max_mitigations": 3,
        "min_quality": 4.0,
    },
}


def _zone_policy(timeframe):
    return ZONE_POLICIES.get(
        timeframe,
        ZONE_POLICIES["1H"],
    )


def _base_candle_stats(
    df,
    atr,
    index,
):
    high = float(
        df["high"].iloc[index]
    )

    low = float(
        df["low"].iloc[index]
    )

    open_price = float(
        df["open"].iloc[index]
    )

    close_price = float(
        df["close"].iloc[index]
    )

    candle_range = max(
        high - low,
        1e-9,
    )

    body = abs(
        close_price - open_price
    )

    atr_value = float(
        atr.iloc[index]
    )

    compact = (
        pd.notna(atr_value)
        and atr_value > 0
        and candle_range
        <= 1.35 * atr_value
        and body / candle_range
        <= 0.70
    )

    return {
        "high": high,
        "low": low,
        "open": open_price,
        "close": close_price,
        "range": candle_range,
        "body_ratio": (
            body / candle_range
        ),
        "atr": atr_value,
        "compact": bool(
            compact
        ),
    }


def _ranges_overlap(
    low_a,
    high_a,
    low_b,
    high_b,
):
    overlap = max(
        0.0,
        min(
            high_a,
            high_b,
        )
        - max(
            low_a,
            low_b,
        ),
    )

    smaller = max(
        1e-9,
        min(
            high_a - low_a,
            high_b - low_b,
        ),
    )

    return (
        overlap / smaller
    )


def _find_base_cluster(
    df,
    atr,
    departure_start,
    max_base_candles=3,
):
    """
    Price Action base:
    contiguous 1-3 candles immediately before departure.

    We prefer compact, overlapping candles. The candle immediately
    before departure can still be used as a one-candle base when it
    is slightly wider, because many valid DBR/RBD origins are not
    textbook dojis.
    """

    last_index = (
        departure_start - 1
    )

    if last_index < 0:
        return None

    last_stats = (
        _base_candle_stats(
            df,
            atr,
            last_index,
        )
    )

    atr_value = (
        last_stats["atr"]
    )

    if (
        pd.isna(atr_value)
        or atr_value <= 0
        or last_stats["range"]
        > 1.75 * atr_value
    ):
        return None

    indices = [
        last_index
    ]

    cluster_low = (
        last_stats["low"]
    )

    cluster_high = (
        last_stats["high"]
    )

    compact_count = (
        1
        if last_stats["compact"]
        else 0
    )

    for index in range(
        last_index - 1,
        max(
            -1,
            last_index
            - max_base_candles,
        ),
        -1,
    ):
        stats = (
            _base_candle_stats(
                df,
                atr,
                index,
            )
        )

        if not stats["compact"]:
            break

        overlap_ratio = (
            _ranges_overlap(
                stats["low"],
                stats["high"],
                cluster_low,
                cluster_high,
            )
        )

        proposed_low = min(
            cluster_low,
            stats["low"],
        )

        proposed_high = max(
            cluster_high,
            stats["high"],
        )

        ref_atr = float(
            atr.iloc[
                last_index
            ]
        )

        if (
            overlap_ratio < 0.20
            or proposed_high
            - proposed_low
            > 1.90 * ref_atr
        ):
            break

        indices.insert(
            0,
            index,
        )

        cluster_low = (
            proposed_low
        )

        cluster_high = (
            proposed_high
        )

        compact_count += 1

    body_high = max(
        max(
            float(
                df["open"].iloc[i]
            ),
            float(
                df["close"].iloc[i]
            ),
        )
        for i in indices
    )

    body_low = min(
        min(
            float(
                df["open"].iloc[i]
            ),
            float(
                df["close"].iloc[i]
            ),
        )
        for i in indices
    )

    clean_base = (
        compact_count
        == len(indices)
        and (
            cluster_high
            - cluster_low
        )
        <= 1.60
        * float(
            atr.iloc[last_index]
        )
    )

    return {
        "start_index": (
            indices[0]
        ),
        "end_index": (
            indices[-1]
        ),
        "indices": indices,
        "count": len(indices),
        "low": cluster_low,
        "high": cluster_high,
        "body_high": body_high,
        "body_low": body_low,
        "clean": bool(
            clean_base
        ),
    }


def _incoming_leg(
    df,
    atr,
    base_start,
    lookback=3,
):
    if base_start <= 0:
        return "neutral"

    first = max(
        0,
        base_start
        - lookback,
    )

    start_close = float(
        df["close"].iloc[first]
    )

    end_close = float(
        df["close"].iloc[
            base_start - 1
        ]
    )

    atr_value = float(
        atr.iloc[
            base_start - 1
        ]
    )

    if (
        pd.isna(atr_value)
        or atr_value <= 0
    ):
        return "neutral"

    move = (
        end_close
        - start_close
    )

    if move >= (
        0.30 * atr_value
    ):
        return "rally"

    if move <= (
        -0.30 * atr_value
    ):
        return "drop"

    return "neutral"


def _zone_pattern(
    incoming,
    direction,
):
    if direction == "bullish":
        return (
            "DBR"
            if incoming == "drop"
            else "RBR"
        )

    return (
        "RBD"
        if incoming == "rally"
        else "DBD"
    )


def _zone_departure_candidates(
    df,
    atr,
    strict_displacements,
):
    """
    Zone-specific departure detector.

    V3 keeps the strict single-candle displacement used by the setup
    engine and additionally detects clean 2-3 candle directional legs.
    This lets Supply/Demand reflect Price Action origins without making
    the trading setup engine artificially more permissive.
    """

    candidates = []

    strict_indices = set()

    for event in (
        strict_displacements
    ):
        item = dict(event)

        item[
            "start_index"
        ] = int(
            event["index"]
        )

        item[
            "end_index"
        ] = int(
            event["index"]
        )

        item[
            "departure_type"
        ] = "single"

        candidates.append(item)

        strict_indices.add(
            int(
                event["index"]
            )
        )

    for length in (2, 3):
        for end_index in range(
            length - 1,
            len(df),
        ):
            start_index = (
                end_index
                - length
                + 1
            )

            if any(
                index
                in strict_indices
                for index in range(
                    start_index,
                    end_index + 1,
                )
            ):
                continue

            atr_value = float(
                atr.iloc[
                    end_index
                ]
            )

            if (
                pd.isna(atr_value)
                or atr_value <= 0
            ):
                continue

            open_start = float(
                df["open"].iloc[
                    start_index
                ]
            )

            close_end = float(
                df["close"].iloc[
                    end_index
                ]
            )

            leg_high = float(
                df["high"].iloc[
                    start_index:
                    end_index + 1
                ].max()
            )

            leg_low = float(
                df["low"].iloc[
                    start_index:
                    end_index + 1
                ].min()
            )

            leg_range = max(
                leg_high - leg_low,
                1e-9,
            )

            move = (
                close_end
                - open_start
            )

            strength = (
                abs(move)
                / atr_value
            )

            directional_efficiency = (
                abs(move)
                / leg_range
            )

            if (
                strength < 1.55
                or directional_efficiency
                < 0.62
            ):
                continue

            direction = (
                "bullish"
                if move > 0
                else "bearish"
            )

            closes = [
                float(
                    df["close"].iloc[i]
                )
                for i in range(
                    start_index,
                    end_index + 1,
                )
            ]

            opens = [
                float(
                    df["open"].iloc[i]
                )
                for i in range(
                    start_index,
                    end_index + 1,
                )
            ]

            directional_bars = sum(
                1
                for open_price, close_price
                in zip(opens, closes)
                if (
                    close_price
                    > open_price
                    if direction
                    == "bullish"
                    else close_price
                    < open_price
                )
            )

            if directional_bars < (
                length - 1
            ):
                continue

            close_location = (
                (
                    close_end
                    - leg_low
                )
                / leg_range
            )

            if (
                direction == "bullish"
                and close_location < 0.72
            ):
                continue

            if (
                direction == "bearish"
                and close_location > 0.28
            ):
                continue

            candidates.append(
                {
                    "index": end_index,
                    "time": (
                        df.index[
                            end_index
                        ].isoformat()
                    ),
                    "direction": (
                        direction
                    ),
                    "strength": (
                        strength
                    ),
                    "open": (
                        open_start
                    ),
                    "close": (
                        close_end
                    ),
                    "high": (
                        leg_high
                    ),
                    "low": (
                        leg_low
                    ),
                    "start_index": (
                        start_index
                    ),
                    "end_index": (
                        end_index
                    ),
                    "departure_type": (
                        f"leg{length}"
                    ),
                }
            )

    candidates.sort(
        key=lambda item: (
            item[
                "end_index"
            ],
            item[
                "strength"
            ],
        )
    )

    deduped = []

    for candidate in candidates:
        duplicate = False

        for existing in (
            deduped[-4:]
        ):
            if (
                existing[
                    "direction"
                ]
                == candidate[
                    "direction"
                ]
                and abs(
                    existing[
                        "end_index"
                    ]
                    - candidate[
                        "end_index"
                    ]
                )
                <= 1
            ):
                if (
                    candidate[
                        "strength"
                    ]
                    > existing[
                        "strength"
                    ]
                ):
                    deduped.remove(
                        existing
                    )

                    break

                duplicate = True

                break

        if not duplicate:
            deduped.append(
                candidate
            )

    return deduped


def _zone_mitigation_count(
    df,
    departure_end,
    lower,
    upper,
    side,
):
    """
    Count separate revisits after the departure has finished.
    Consecutive overlapping candles count as one mitigation.
    """

    mitigations = 0

    in_touch = False

    invalidated = False

    for i in range(
        departure_end + 1,
        len(df),
    ):
        high_now = float(
            df["high"].iloc[i]
        )

        low_now = float(
            df["low"].iloc[i]
        )

        close_now = float(
            df["close"].iloc[i]
        )

        if side == "demand":
            invalidated = (
                close_now < lower
            )
        else:
            invalidated = (
                close_now > upper
            )

        if invalidated:
            break

        overlaps = (
            high_now >= lower
            and low_now <= upper
        )

        if (
            overlaps
            and not in_touch
        ):
            mitigations += 1

        in_touch = overlaps

    return (
        mitigations,
        invalidated,
    )


def _first_structure_after(
    events,
    start_index,
    end_index,
    direction,
):
    matches = [
        event
        for event in events
        if (
            event.get(
                "direction"
            )
            == direction
            and start_index
            <= event.get(
                "index",
                -1,
            )
            <= end_index
        )
    ]

    return (
        matches[0]
        if matches
        else None
    )


def _fvg_near(
    fvgs,
    start_index,
    end_index,
    direction,
):
    return any(
        fvg.get("type")
        == direction
        and start_index
        <= fvg.get(
            "index",
            -1,
        )
        <= end_index
        for fvg in fvgs
    )


def _sweep_before(
    sweeps,
    base_index,
    departure_end,
    direction,
):
    return any(
        sweep.get("direction")
        == direction
        and (
            base_index - 10
            <= sweep.get(
                "index",
                -1,
            )
            <= departure_end
        )
        for sweep in sweeps
    )


def _departure_excursion(
    df,
    atr,
    departure_end,
    lower,
    upper,
    side,
    bars=6,
):
    end = min(
        len(df),
        departure_end
        + bars
        + 1,
    )

    future = df.iloc[
        departure_end:
        end
    ]

    if future.empty:
        return 0.0

    atr_value = float(
        atr.iloc[
            departure_end
        ]
    )

    if (
        pd.isna(atr_value)
        or atr_value <= 0
    ):
        return 0.0

    if side == "demand":
        excursion = (
            float(
                future[
                    "high"
                ].max()
            )
            - upper
        )
    else:
        excursion = (
            lower
            - float(
                future[
                    "low"
                ].min()
            )
        )

    return max(
        0.0,
        excursion
        / atr_value,
    )


def _fast_return_to_zone(
    df,
    departure_end,
    lower,
    upper,
    bars=3,
):
    end = min(
        len(df),
        departure_end
        + bars
        + 1,
    )

    for i in range(
        departure_end + 1,
        end,
    ):
        high_now = float(
            df["high"].iloc[i]
        )

        low_now = float(
            df["low"].iloc[i]
        )

        if (
            high_now >= lower
            and low_now <= upper
        ):
            return True

    return False


def _quality_label(score):
    if score >= 7.0:
        return "HIGH"

    if score >= 5.0:
        return "MEDIUM"

    return "LOW"


def _zone_grade(score):
    """
    Descriptive calibration bucket for later evaluation.

    A+/A are the strongest structural origins under the current
    scoring model. B/C remain visible and valid when they satisfy
    the existing V3 policy; the grade is not yet used to rewrite
    historical thresholds before enough outcome data exists.
    """

    if score >= 8.5:
        return "A+"

    if score >= 7.0:
        return "A"

    if score >= 5.0:
        return "B"

    return "C"


def _rejection_reasons(
    invalidated,
    age_bars,
    policy,
    mitigations,
    distance_pct,
    quality_score,
    departure_excursion,
):
    reasons = []

    if invalidated:
        reasons.append(
            "invalidated"
        )

    if (
        age_bars
        > policy[
            "max_age"
        ]
    ):
        reasons.append(
            "too_old"
        )

    if (
        mitigations
        > policy[
            "max_mitigations"
        ]
    ):
        reasons.append(
            "over_mitigated"
        )

    if (
        distance_pct
        > policy[
            "max_distance_pct"
        ]
    ):
        reasons.append(
            "too_far"
        )

    if departure_excursion < 0.90:
        reasons.append(
            "weak_departure"
        )

    if (
        quality_score
        < policy[
            "min_quality"
        ]
    ):
        reasons.append(
            "low_quality"
        )

    return reasons


def _find_zones(
    df,
    atr,
    structure_events,
    fvgs,
    sweeps,
    timeframe="1H",
):
    """
    Supply/Demand V3 - Price Action origin zones.

    1. Detect a strong single-candle displacement OR a clean 2-3 candle
       directional departure leg.
    2. Trace backward to a contiguous 1-3 candle base.
    3. Build DBR/RBR Demand or RBD/DBD Supply from the full base.
    4. Score structural impact, FVG, liquidity sweep, departure quality,
       base quality, freshness, mitigation, age and distance.
    5. Keep old but fresh structural origins; age is a penalty/filter,
       not a requirement for a recently-created zone.
    """

    policy = (
        _zone_policy(
            timeframe
        )
    )

    strict_displacements = (
        _find_displacements(
            df,
            atr,
        )
    )

    departures = (
        _zone_departure_candidates(
            df,
            atr,
            strict_displacements,
        )
    )

    current_index = (
        len(df) - 1
    )

    current_price = float(
        df["close"].iloc[-1]
    )

    supply_all = []

    demand_all = []

    for departure in departures:
        direction = (
            departure[
                "direction"
            ]
        )

        departure_start = int(
            departure[
                "start_index"
            ]
        )

        departure_end = int(
            departure[
                "end_index"
            ]
        )

        if departure_start <= 0:
            continue

        base = (
            _find_base_cluster(
                df,
                atr,
                departure_start,
            )
        )

        if not base:
            continue

        side = (
            "demand"
            if direction == "bullish"
            else "supply"
        )

        if side == "demand":
            lower = float(
                base["low"]
            )

            upper = float(
                base[
                    "body_high"
                ]
            )
        else:
            lower = float(
                base[
                    "body_low"
                ]
            )

            upper = float(
                base["high"]
            )

        if upper <= lower:
            continue

        incoming = (
            _incoming_leg(
                df,
                atr,
                base[
                    "start_index"
                ],
            )
        )

        pattern = (
            _zone_pattern(
                incoming,
                direction,
            )
        )

        (
            mitigations,
            invalidated,
        ) = (
            _zone_mitigation_count(
                df,
                departure_end,
                lower,
                upper,
                side,
            )
        )

        age_bars = (
            current_index
            - base[
                "start_index"
            ]
        )

        midpoint = (
            lower + upper
        ) / 2

        distance_pct = (
            abs(
                midpoint
                - current_price
            )
            / max(
                current_price,
                1e-9,
            )
        )

        structure_event = (
            _first_structure_after(
                structure_events,
                departure_end,
                min(
                    current_index,
                    departure_end
                    + 10,
                ),
                direction,
            )
        )

        structure_confirmed = (
            structure_event
            is not None
        )

        structure_kind = (
            structure_event.get(
                "kind"
            )
            if structure_event
            else None
        )

        fvg_confirmed = (
            _fvg_near(
                fvgs,
                departure_start,
                min(
                    current_index,
                    departure_end
                    + 4,
                ),
                direction,
            )
        )

        sweep_context = (
            _sweep_before(
                sweeps,
                base[
                    "start_index"
                ],
                departure_end,
                direction,
            )
        )

        departure_excursion = (
            _departure_excursion(
                df,
                atr,
                departure_end,
                lower,
                upper,
                side,
            )
        )

        fast_return = (
            _fast_return_to_zone(
                df,
                departure_end,
                lower,
                upper,
            )
        )

        strength = float(
            departure.get(
                "strength",
                1.0,
            )
        )

        departure_score = min(
            2.25,
            0.75
            + max(
                0.0,
                strength - 1.0,
            )
            * 1.25,
        )

        excursion_score = min(
            1.75,
            departure_excursion
            * 0.60,
        )

        if mitigations == 0:
            freshness_score = 2.0
        elif mitigations == 1:
            freshness_score = 1.20
        elif mitigations == 2:
            freshness_score = 0.65
        else:
            freshness_score = 0.25

        age_score = (
            1.0
            if age_bars
            <= policy[
                "primary_age"
            ]
            else 0.45
        )

        structure_score = (
            2.0
            if structure_confirmed
            else 0.0
        )

        if (
            structure_kind
            == "CHoCH"
        ):
            structure_score += (
                0.25
            )

        base_score = (
            1.0
            if base["clean"]
            else 0.40
        )

        quality_score = (
            departure_score
            + excursion_score
            + freshness_score
            + age_score
            + structure_score
            + (
                1.0
                if fvg_confirmed
                else 0.0
            )
            + (
                1.0
                if sweep_context
                else 0.0
            )
            + base_score
            - (
                0.40
                * mitigations
            )
            - (
                1.0
                if fast_return
                else 0.0
            )
        )

        rank_score = (
            quality_score
            - (
                distance_pct
                * 2.0
            )
            - (
                mitigations
                * 0.20
            )
        )

        reasons = (
            _rejection_reasons(
                invalidated,
                age_bars,
                policy,
                mitigations,
                distance_pct,
                quality_score,
                departure_excursion,
            )
        )

        qualified = (
            len(reasons) == 0
        )

        zone = {
            "index": (
                base[
                    "start_index"
                ]
            ),
            "end_index": (
                base[
                    "end_index"
                ]
            ),
            "time": (
                df.index[
                    base[
                        "start_index"
                    ]
                ].isoformat()
            ),
            "lower": lower,
            "upper": upper,
            "invalidated": (
                invalidated
            ),
            "qualified": (
                qualified
            ),
            "rejection_reasons": (
                reasons
            ),
            "pattern": pattern,
            "incoming_leg": (
                incoming
            ),
            "base_count": (
                base["count"]
            ),
            "clean_base": (
                base["clean"]
            ),
            "departure_type": (
                departure[
                    "departure_type"
                ]
            ),
            "departure_start": (
                departure_start
            ),
            "departure_end": (
                departure_end
            ),
            "departure_excursion_atr": (
                departure_excursion
            ),
            "fast_return": (
                fast_return
            ),
            "strength": strength,
            "mitigations": (
                mitigations
            ),
            "age_bars": (
                age_bars
            ),
            "distance_pct": (
                distance_pct
            ),
            "structure_confirmed": (
                structure_confirmed
            ),
            "structure_kind": (
                structure_kind
            ),
            "fvg_confirmed": (
                fvg_confirmed
            ),
            "sweep_context": (
                sweep_context
            ),
            "quality_score": (
                quality_score
            ),
            "quality": (
                _quality_label(
                    quality_score
                )
            ),
            "grade": (
                _zone_grade(
                    quality_score
                )
            ),
            "rank_score": (
                rank_score
            ),
            "timeframe": timeframe,
        }

        if side == "demand":
            demand_all.append(
                zone
            )
        else:
            supply_all.append(
                zone
            )

    def distance_to_price(
        zone,
    ):
        lower = float(
            zone["lower"]
        )

        upper = float(
            zone["upper"]
        )

        if (
            lower
            <= current_price
            <= upper
        ):
            return 0.0

        return min(
            abs(
                current_price
                - lower
            ),
            abs(
                current_price
                - upper
            ),
        )

    def ranked(
        zones,
        qualified,
    ):
        selected = [
            zone
            for zone in zones
            if bool(
                zone.get(
                    "qualified",
                    False,
                )
            )
            == qualified
        ]

        selected.sort(
            key=lambda zone: (
                zone.get(
                    "rank_score",
                    0,
                )
            ),
            reverse=True,
        )

        return selected

    def prioritize_active(
        zones,
    ):
        """
        Primary = nearest valid zone to current price.
        Secondary = highest-quality remaining valid zone.
        Remaining zones stay quality-ranked for diagnostics.
        """

        if not zones:
            return []

        primary = min(
            zones,
            key=distance_to_price,
        )

        remaining = [
            zone
            for zone in zones
            if zone is not primary
        ]

        remaining.sort(
            key=lambda zone: (
                zone.get(
                    "rank_score",
                    0,
                )
            ),
            reverse=True,
        )

        return [
            primary,
            *remaining,
        ]

    def reference_zones(
        rejected,
    ):
        """
        Historical/reference zones are not active trading zones.
        Keep only zones rejected solely because they were mitigated
        too many times; invalidated, weak, stale or distant zones
        stay hidden from the chart.
        """

        references = [
            zone
            for zone in rejected
            if (
                not zone.get(
                    "invalidated",
                    False,
                )
                and set(
                    zone.get(
                        "rejection_reasons",
                        [],
                    )
                )
                == {
                    "over_mitigated"
                }
            )
        ]

        references.sort(
            key=lambda zone: (
                distance_to_price(
                    zone
                ),
                -float(
                    zone.get(
                        "quality_score",
                        0,
                    )
                ),
            )
        )

        return references

    active_supply = ranked(
        supply_all,
        True,
    )

    active_demand = ranked(
        demand_all,
        True,
    )

    rejected_supply = ranked(
        supply_all,
        False,
    )

    rejected_demand = ranked(
        demand_all,
        False,
    )

    return (
        prioritize_active(
            active_supply
        ),
        prioritize_active(
            active_demand
        ),
        strict_displacements,
        rejected_supply,
        rejected_demand,
        reference_zones(
            rejected_supply
        ),
        reference_zones(
            rejected_demand
        ),
    )


def _primary_zone(
    zones,
):
    if not zones:
        return None

    return zones[0]


def _latest_recent(
    events,
    current_index,
    bars,
    direction=None,
):
    candidates = [
        item
        for item in events
        if (
            current_index
            - item["index"]
            <= bars
        )
        and (
            direction is None
            or item.get(
                "direction"
            )
            == direction
        )
    ]

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda item: item[
            "index"
        ],
    )


def _setup_signal(
    current_index,
    sweeps,
    displacements,
    structure_events,
    retests,
    direction,
):
    latest_opposite_structure = (
        _latest_recent(
            structure_events,
            current_index,
            20,
            (
                "bearish"
                if direction == "bullish"
                else "bullish"
            ),
        )
    )

    cutoff_index = (
        latest_opposite_structure[
            "index"
        ]
        if latest_opposite_structure
        else -1
    )

    def after_cutoff(
        events,
        bars,
    ):
        candidate = _latest_recent(
            events,
            current_index,
            bars,
            direction,
        )

        if (
            candidate
            and candidate["index"]
            <= cutoff_index
        ):
            return None

        return candidate

    sweep = after_cutoff(
        sweeps,
        20,
    )

    displacement = (
        after_cutoff(
            displacements,
            12,
        )
    )

    structure = after_cutoff(
        structure_events,
        12,
    )

    retest = after_cutoff(
        retests,
        8,
    )

    score = sum(
        item is not None
        for item in [
            sweep,
            displacement,
            structure,
            retest,
        ]
    )

    ordered = False

    if (
        displacement
        and structure
        and retest
    ):
        ordered = (
            displacement["index"]
            <= structure["index"]
            < retest["index"]
        )

    sweep_before_structure = (
        sweep is not None
        and structure is not None
        and sweep["index"]
        <= structure["index"]
    )

    confirmed = (
        ordered
        and (
            sweep_before_structure
            or (
                structure.get(
                    "kind"
                )
                == "BOS"
            )
        )
    )

    latest_index = max(
        [
            item["index"]
            for item in [
                sweep,
                displacement,
                structure,
                retest,
            ]
            if item is not None
        ],
        default=-1,
    )

    return {
        "direction": direction,
        "score": score,
        "confirmed": confirmed,
        "latest_index": (
            latest_index
        ),
        "sweep": sweep,
        "displacement": (
            displacement
        ),
        "structure": structure,
        "retest": retest,
    }


def _infer_market_regime(
    trend,
    last_event,
    bullish_setup,
    bearish_setup,
    current_index,
):
    """
    Add transition/pullback context on top of the structural trend.

    Trend remains the deterministic HH/HL vs LH/LL classification.
    Regime explains whether the latest PA is continuing that trend,
    pulling back inside it, or beginning a structural transition.
    """

    bullish_score = int(
        bullish_setup.get(
            "score",
            0,
        )
        or 0
    )

    bearish_score = int(
        bearish_setup.get(
            "score",
            0,
        )
        or 0
    )

    event_direction = (
        last_event.get(
            "direction"
        )
        if last_event
        else None
    )

    event_kind = (
        last_event.get(
            "kind"
        )
        if last_event
        else None
    )

    event_age = (
        current_index
        - int(
            last_event.get(
                "index",
                current_index,
            )
        )
        if last_event
        else None
    )

    event_is_recent = (
        event_age is not None
        and 0 <= event_age <= 20
    )

    if trend == "bullish":
        if (
            event_direction
            == "bearish"
            and event_kind
            == "CHoCH"
            and bearish_score >= 2
        ):
            return (
                "BULLISH_TO_BEARISH_TRANSITION"
            )

        if bearish_score >= 2:
            return "BULLISH_PULLBACK"

        return "BULLISH_TREND"

    if trend == "bearish":
        if (
            event_direction
            == "bullish"
            and event_kind
            == "CHoCH"
            and bullish_score >= 2
        ):
            return (
                "BEARISH_TO_BULLISH_TRANSITION"
            )

        if bullish_score >= 2:
            return "BEARISH_PULLBACK"

        return "BEARISH_TREND"

    if (
        event_is_recent
        and event_kind == "CHoCH"
        and event_direction
        in (
            "bullish",
            "bearish",
        )
    ):
        return (
            "BULLISH_TRANSITION"
            if event_direction
            == "bullish"
            else "BEARISH_TRANSITION"
        )

    if (
        bullish_score >= 2
        and bullish_score
        > bearish_score
    ):
        return "BULLISH_TRANSITION"

    if (
        bearish_score >= 2
        and bearish_score
        > bullish_score
    ):
        return "BEARISH_TRANSITION"

    return "RANGE"


def analyze_smc(
    df,
    swing_window=2,
    timeframe="1H",
):
    """
    Conservative, rule-based SMC analysis.
    """

    if (
        df is None
        or len(df) < 20
    ):
        raise ValueError(
            (
                "Need at least 20 "
                "candles for SMC "
                "analysis"
            )
        )

    work = df[
        [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
    ].copy()

    work = (
        work.dropna().copy()
    )

    atr = _atr(work)

    (
        high_mask,
        low_mask,
    ) = _swing_masks(
        work,
        window=swing_window,
    )

    swing_highs = [
        (
            i,
            float(
                work[
                    "high"
                ].iloc[i]
            ),
            work.index[
                i
            ].isoformat(),
        )
        for i in range(len(work))
        if bool(
            high_mask.iloc[i]
        )
    ]

    swing_lows = [
        (
            i,
            float(
                work[
                    "low"
                ].iloc[i]
            ),
            work.index[
                i
            ].isoformat(),
        )
        for i in range(len(work))
        if bool(
            low_mask.iloc[i]
        )
    ]

    trend = _infer_trend(
        swing_highs,
        swing_lows,
    )

    structure_events = (
        _structure_events(
            work,
            high_mask,
            low_mask,
            window=swing_window,
        )
    )

    sweeps = _find_sweeps(
        work,
        high_mask,
        low_mask,
        window=swing_window,
    )

    fvgs = _find_fvgs(
        work
    )

    active_fvgs = [
        fvg
        for fvg in fvgs
        if not fvg["filled"]
    ]

    (
        supply,
        demand,
        displacements,
        rejected_supply,
        rejected_demand,
        reference_supply,
        reference_demand,
    ) = (
        _find_zones(
            work,
            atr,
            structure_events,
            fvgs,
            sweeps,
            timeframe=timeframe,
        )
    )

    retests = _find_retests(
        work,
        structure_events,
        atr,
    )

    current_price = float(
        work["close"].iloc[-1]
    )

    bsl, ssl = _find_liquidity(
        work,
        current_price,
        swing_highs,
        swing_lows,
    )

    current_index = (
        len(work) - 1
    )

    nearest_supply = (
        _primary_zone(
            supply
        )
    )

    nearest_demand = (
        _primary_zone(
            demand
        )
    )

    last_event = (
        structure_events[-1]
        if structure_events
        else None
    )

    bullish_setup = (
        _setup_signal(
            current_index,
            sweeps,
            displacements,
            structure_events,
            retests,
            "bullish",
        )
    )

    bearish_setup = (
        _setup_signal(
            current_index,
            sweeps,
            displacements,
            structure_events,
            retests,
            "bearish",
        )
    )

    preferred_setup = max(
        [
            bullish_setup,
            bearish_setup,
        ],
        key=lambda item: (
            item["score"],
            item["latest_index"],
        ),
    )

    regime = (
        _infer_market_regime(
            trend,
            last_event,
            bullish_setup,
            bearish_setup,
            current_index,
        )
    )

    return {
        "current_price": (
            current_price
        ),
        "timeframe": timeframe,
        "zone_policy": (
            _zone_policy(
                timeframe
            )
        ),
        "atr": float(
            atr.iloc[-1]
        ),
        "trend": trend,
        "regime": regime,
        "bsl": bsl,
        "ssl": ssl,
        "swing_highs": [
            {
                "index": index,
                "price": price,
                "time": timestamp,
            }
            for (
                index,
                price,
                timestamp,
            )
            in swing_highs[-8:]
        ],
        "swing_lows": [
            {
                "index": index,
                "price": price,
                "time": timestamp,
            }
            for (
                index,
                price,
                timestamp,
            )
            in swing_lows[-8:]
        ],
        "events": (
            structure_events[
                -10:
            ]
        ),
        "last_event": (
            last_event
        ),
        "sweeps": (
            sweeps[-8:]
        ),
        "last_sweep": (
            sweeps[-1]
            if sweeps
            else None
        ),
        "displacements": (
            displacements[-8:]
        ),
        "last_displacement": (
            displacements[-1]
            if displacements
            else None
        ),
        "retests": (
            retests[-8:]
        ),
        "last_retest": (
            retests[-1]
            if retests
            else None
        ),
        "active_fvgs": (
            active_fvgs[-6:]
        ),
        "nearest_supply": (
            nearest_supply
        ),
        "nearest_demand": (
            nearest_demand
        ),
        "supply_zones": (
            supply[:4]
        ),
        "demand_zones": (
            demand[:4]
        ),
        "rejected_supply_zones": (
            rejected_supply[:6]
        ),
        "rejected_demand_zones": (
            rejected_demand[:6]
        ),
        "reference_supply_zones": (
            reference_supply[:2]
        ),
        "reference_demand_zones": (
            reference_demand[:2]
        ),
        "zone_engine": "V3_PRICE_ACTION",
        "setup": (
            preferred_setup
        ),
        "bullish_setup": (
            bullish_setup
        ),
        "bearish_setup": (
            bearish_setup
        ),
    }


def derive_overall_status(
    analysis_4h,
    analysis_1h,
    analysis_15m,
):
    """
    Signal state is owned by the 15M execution sequence.

    Higher-timeframe disagreement is reported separately through
    derive_mtf_alignment() and enforced by the Trade Plan execution
    gate. This keeps CONFIRMED = sequence confirmed, not "trade now".
    """

    setup_15m = (
        analysis_15m.get(
            "setup",
            {},
        )
    )

    direction = (
        setup_15m.get(
            "direction"
        )
    )

    score = int(
        setup_15m.get(
            "score",
            0,
        )
        or 0
    )

    confirmed = bool(
        setup_15m.get(
            "confirmed",
            False,
        )
    )

    if direction == "bullish":
        if confirmed:
            return "CONFIRMED LONG"

        if score >= 2:
            return "DEVELOPING LONG"

    if direction == "bearish":
        if confirmed:
            return "CONFIRMED SHORT"

        if score >= 2:
            return "DEVELOPING SHORT"

    return "WAIT"


def _regime_direction(
    analysis,
):
    regime = str(
        analysis.get(
            "regime",
            ""
        )
        or ""
    )

    bullish_regimes = {
        "BULLISH_TREND",
        "BEARISH_TO_BULLISH_TRANSITION",
        "BULLISH_TRANSITION",
    }

    bearish_regimes = {
        "BEARISH_TREND",
        "BULLISH_TO_BEARISH_TRANSITION",
        "BEARISH_TRANSITION",
    }

    if regime in bullish_regimes:
        return "bullish"

    if regime in bearish_regimes:
        return "bearish"

    # Pullbacks preserve the parent structural direction but are
    # treated as softer context by the alignment engine.
    if regime == "BULLISH_PULLBACK":
        return "bullish_soft"

    if regime == "BEARISH_PULLBACK":
        return "bearish_soft"

    trend = analysis.get(
        "trend"
    )

    if trend in (
        "bullish",
        "bearish",
    ):
        return trend

    return "neutral"


def derive_mtf_alignment(
    analysis_4h,
    analysis_1h,
    analysis_15m,
):
    """
    Explain whether higher-timeframe context supports the active 15M
    setup. This does not manufacture a setup and does not change the
    15M signal sequence; it is an execution-quality filter.
    """

    setup_15m = (
        analysis_15m.get(
            "setup",
            {},
        )
    )

    direction = (
        setup_15m.get(
            "direction"
        )
    )

    if direction not in (
        "bullish",
        "bearish",
    ):
        return {
            "label": "NEUTRAL",
            "direction": direction,
            "supporters": [],
            "conflicts": [],
            "notes": [
                "No directional 15M setup"
            ],
        }

    opposite = (
        "bearish"
        if direction == "bullish"
        else "bullish"
    )

    supporters = []
    conflicts = []
    notes = []

    for timeframe, analysis in [
        ("4H", analysis_4h),
        ("1H", analysis_1h),
    ]:
        context = (
            _regime_direction(
                analysis
            )
        )

        if context == direction:
            supporters.append(
                f"{timeframe} regime"
            )

        elif context == opposite:
            conflicts.append(
                f"{timeframe} regime"
            )

        elif context == (
            direction + "_soft"
        ):
            supporters.append(
                f"{timeframe} parent trend"
            )

            notes.append(
                (
                    f"{timeframe} is in a "
                    "counter-move/pullback state"
                )
            )

        elif context == (
            opposite + "_soft"
        ):
            conflicts.append(
                f"{timeframe} parent trend"
            )

            notes.append(
                (
                    f"{timeframe} is in a "
                    "counter-move/pullback state"
                )
            )

    one_hour_setup = (
        analysis_1h.get(
            "setup",
            {},
        )
    )

    one_hour_score = int(
        one_hour_setup.get(
            "score",
            0,
        )
        or 0
    )

    one_hour_direction = (
        one_hour_setup.get(
            "direction"
        )
    )

    if (
        one_hour_score >= 2
        and one_hour_direction
        == direction
    ):
        supporters.append(
            "1H active setup"
        )

    if (
        one_hour_score >= 2
        and one_hour_direction
        == opposite
    ):
        conflicts.append(
            "1H active setup"
        )

    # A live opposite 1H setup is a hard conflict. Two independent
    # higher-timeframe conflicts are also treated as hard conflict.
    hard_conflict = (
        "1H active setup"
        in conflicts
        or len(
            set(conflicts)
        ) >= 2
    )

    if hard_conflict:
        label = "CONFLICT"

    elif (
        supporters
        and not conflicts
    ):
        label = "ALIGNED"

    else:
        label = "PARTIAL"

    return {
        "label": label,
        "direction": direction,
        "supporters": sorted(
            set(supporters)
        ),
        "conflicts": sorted(
            set(conflicts)
        ),
        "notes": notes,
    }
