def _price(point):
    if not point:
        return None
    return point.get("price")


def _zone_distance(zone, current_price):
    if not zone:
        return None

    lower = float(zone["lower"])
    upper = float(zone["upper"])

    if lower <= current_price <= upper:
        return 0.0

    if current_price < lower:
        return lower - current_price

    return current_price - upper


def _nearby_fvg(analysis, direction, current_price, atr):
    wanted = (
        "bullish"
        if direction == "long"
        else "bearish"
    )

    candidates = []

    for fvg in analysis.get(
        "active_fvgs",
        [],
    ):
        if fvg.get("type") != wanted:
            continue

        lower = float(
            fvg["lower"]
        )

        upper = float(
            fvg["upper"]
        )

        if (
            current_price
            < lower
        ):
            distance = (
                lower
                - current_price
            )

        elif (
            current_price
            > upper
        ):
            distance = (
                current_price
                - upper
            )

        else:
            distance = 0.0

        if distance <= 1.5 * atr:
            candidates.append(
                (
                    distance,
                    lower,
                    upper,
                )
            )

    if not candidates:
        return None

    _, lower, upper = min(
        candidates,
        key=lambda item: item[0],
    )

    return {
        "lower": lower,
        "upper": upper,
        "source": (
            f"{wanted} FVG"
        ),
    }


def _candidate_entry_zones(
    analysis_15m,
    direction,
):
    current_price = float(
        analysis_15m[
            "current_price"
        ]
    )

    atr = max(
        float(
            analysis_15m.get(
                "atr",
                0,
            )
        ),
        0.001,
    )

    setup = analysis_15m.get(
        "setup",
        {},
    )

    candidates = []

    retest = setup.get(
        "retest"
    )

    structure = setup.get(
        "structure"
    )

    if retest:
        center = float(
            retest["level"]
        )

        half = max(
            atr * 0.10,
            center * 0.0005,
        )

        candidates.append(
            {
                "lower": (
                    center - half
                ),
                "upper": (
                    center + half
                ),
                "source": "retest",
                "priority": 0,
            }
        )

    zone = (
        analysis_15m.get(
            "nearest_demand"
        )
        if direction == "long"
        else analysis_15m.get(
            "nearest_supply"
        )
    )

    if zone:
        distance = _zone_distance(
            zone,
            current_price,
        )

        if (
            distance is not None
            and distance <= 1.5 * atr
        ):
            candidates.append(
                {
                    "lower": float(
                        zone["lower"]
                    ),
                    "upper": float(
                        zone["upper"]
                    ),
                    "source": (
                        "demand"
                        if direction
                        == "long"
                        else "supply"
                    ),
                    "zone_grade": (
                        zone.get(
                            "grade"
                        )
                    ),
                    "zone_quality": (
                        zone.get(
                            "quality"
                        )
                    ),
                    "priority": 1,
                }
            )

    fvg = _nearby_fvg(
        analysis_15m,
        direction,
        current_price,
        atr,
    )

    if fvg:
        fvg["priority"] = 2
        candidates.append(
            fvg
        )

    if structure:
        center = float(
            structure["level"]
        )

        half = max(
            atr * 0.08,
            center * 0.0004,
        )

        candidates.append(
            {
                "lower": (
                    center - half
                ),
                "upper": (
                    center + half
                ),
                "source": (
                    f"{structure['kind']} "
                    "reclaim"
                ),
                "priority": 3,
            }
        )

    if not candidates:
        half = atr * 0.12

        candidates.append(
            {
                "lower": (
                    current_price - half
                ),
                "upper": (
                    current_price + half
                ),
                "source": (
                    "ATR pullback"
                ),
                "priority": 4,
            }
        )

    def score(candidate):
        distance = _zone_distance(
            candidate,
            current_price,
        )

        # Prefer a retracement zone on the correct side of price.
        if direction == "long":
            wrong_side = (
                candidate["lower"]
                > current_price
            )
        else:
            wrong_side = (
                candidate["upper"]
                < current_price
            )

        return (
            1 if wrong_side else 0,
            candidate["priority"],
            distance,
        )

    best = min(
        candidates,
        key=score,
    )

    best = dict(best)

    best.pop(
        "priority",
        None,
    )

    return best


def _build_stop(
    analysis_15m,
    entry_zone,
    direction,
):
    atr = max(
        float(
            analysis_15m.get(
                "atr",
                0,
            )
        ),
        0.001,
    )

    setup = analysis_15m.get(
        "setup",
        {},
    )

    sweep = setup.get(
        "sweep"
    )

    if direction == "long":
        references = [
            float(
                entry_zone["lower"]
            )
        ]

        ssl = _price(
            analysis_15m.get(
                "ssl"
            )
        )

        if ssl is not None:
            references.append(
                float(ssl)
            )

        demand = (
            analysis_15m.get(
                "nearest_demand"
            )
        )

        if demand:
            references.append(
                float(
                    demand["lower"]
                )
            )

        if (
            sweep
            and sweep.get(
                "direction"
            )
            == "bullish"
        ):
            references.append(
                float(
                    sweep.get(
                        "extreme",
                        sweep["level"],
                    )
                )
            )

        invalidation = min(
            references
        )

        return (
            invalidation
            - 0.10 * atr
        )

    references = [
        float(
            entry_zone["upper"]
        )
    ]

    bsl = _price(
        analysis_15m.get(
            "bsl"
        )
    )

    if bsl is not None:
        references.append(
            float(bsl)
        )

    supply = (
        analysis_15m.get(
            "nearest_supply"
        )
    )

    if supply:
        references.append(
            float(
                supply["upper"]
            )
        )

    if (
        sweep
        and sweep.get(
            "direction"
        )
        == "bearish"
    ):
        references.append(
            float(
                sweep.get(
                    "extreme",
                    sweep["level"],
                )
            )
        )

    invalidation = max(
        references
    )

    return (
        invalidation
        + 0.10 * atr
    )


def _liquidity_targets(
    analysis_4h,
    analysis_1h,
    analysis_15m,
    direction,
    entry_mid,
    risk,
):
    candidates = []

    if direction == "long":
        raw = [
            (
                _price(
                    analysis_15m.get(
                        "bsl"
                    )
                ),
                "15M BSL",
            ),
            (
                _price(
                    analysis_1h.get(
                        "bsl"
                    )
                ),
                "1H BSL",
            ),
            (
                _price(
                    analysis_4h.get(
                        "bsl"
                    )
                ),
                "4H BSL",
            ),
        ]

        for value, source in raw:
            if (
                value is not None
                and float(value)
                > entry_mid
            ):
                candidates.append(
                    (
                        float(value),
                        source,
                    )
                )

        candidates.sort(
            key=lambda item: (
                item[0]
            )
        )

    else:
        raw = [
            (
                _price(
                    analysis_15m.get(
                        "ssl"
                    )
                ),
                "15M SSL",
            ),
            (
                _price(
                    analysis_1h.get(
                        "ssl"
                    )
                ),
                "1H SSL",
            ),
            (
                _price(
                    analysis_4h.get(
                        "ssl"
                    )
                ),
                "4H SSL",
            ),
        ]

        for value, source in raw:
            if (
                value is not None
                and float(value)
                < entry_mid
            ):
                candidates.append(
                    (
                        float(value),
                        source,
                    )
                )

        candidates.sort(
            key=lambda item: (
                -item[0]
            )
        )

    deduped = []

    for value, source in candidates:
        if not any(
            abs(
                value
                - item[0]
            )
            < 1e-8
            for item
            in deduped
        ):
            deduped.append(
                (
                    value,
                    source,
                )
            )

    rr_multiple = 1

    while len(deduped) < 3:
        if direction == "long":
            value = (
                entry_mid
                + risk
                * rr_multiple
            )
        else:
            value = (
                entry_mid
                - risk
                * rr_multiple
            )

        duplicate = any(
            abs(
                value
                - item[0]
            )
            < 1e-8
            for item
            in deduped
        )

        if deduped:
            last_price = float(
                deduped[-1][0]
            )

            extends_path = (
                value > last_price
                if direction == "long"
                else value < last_price
            )

        else:
            extends_path = True

        if (
            not duplicate
            and extends_path
        ):
            deduped.append(
                (
                    value,
                    f"{rr_multiple}R",
                )
            )

        rr_multiple += 1

    return deduped[:3]


def build_trade_plan(
    analysis_4h,
    analysis_1h,
    analysis_15m,
    status,
    mtf_alignment=None,
):
    """
    Build a rule-based execution map from the active 15M SMC setup.

    This is not an order instruction. It is a deterministic map used
    for charting, monitoring, and comparing setup development over time.
    """

    setup = analysis_15m.get(
        "setup",
        {},
    )

    score = int(
        setup.get(
            "score",
            0,
        )
        or 0
    )

    if (
        status == "WAIT"
        or score < 2
    ):
        return {
            "active": False,
            "status": status,
            "reason": (
                "No active 15M setup "
                "with at least 2/4 signals."
            ),
        }

    direction = (
        "long"
        if "LONG" in status
        else "short"
    )

    entry_zone = (
        _candidate_entry_zones(
            analysis_15m,
            direction,
        )
    )

    entry_mid = (
        float(
            entry_zone["lower"]
        )
        + float(
            entry_zone["upper"]
        )
    ) / 2

    stop_loss = _build_stop(
        analysis_15m,
        entry_zone,
        direction,
    )

    if direction == "long":
        risk = (
            entry_mid
            - stop_loss
        )
    else:
        risk = (
            stop_loss
            - entry_mid
        )

    atr = max(
        float(
            analysis_15m.get(
                "atr",
                0,
            )
        ),
        0.001,
    )

    # Never publish a zero/negative-risk plan.
    if risk <= 0:
        risk = atr * 0.5

        stop_loss = (
            entry_mid - risk
            if direction == "long"
            else entry_mid + risk
        )

    target_pairs = (
        _liquidity_targets(
            analysis_4h,
            analysis_1h,
            analysis_15m,
            direction,
            entry_mid,
            risk,
        )
    )

    targets = []

    for index, (
        value,
        source,
    ) in enumerate(
        target_pairs,
        start=1,
    ):
        reward = (
            value - entry_mid
            if direction == "long"
            else entry_mid - value
        )

        rr = (
            reward / risk
            if risk > 0
            else None
        )

        targets.append(
            {
                "name": (
                    f"TP{index}"
                ),
                "price": value,
                "source": source,
                "rr": rr,
            }
        )

    if setup.get(
        "confirmed",
        False,
    ):
        setup_state = "confirmed"
    elif score >= 3:
        setup_state = "developing"
    else:
        setup_state = "watch"

    first_rr = (
        targets[0].get("rr")
        if targets
        else None
    )

    one_hour_setup = (
        analysis_1h.get(
            "setup",
            {},
        )
    )

    one_hour_direction = (
        one_hour_setup.get(
            "direction"
        )
    )

    one_hour_score = int(
        one_hour_setup.get(
            "score",
            0,
        )
        or 0
    )

    blockers = []

    mtf_alignment = (
        mtf_alignment
        or {}
    )

    if (
        mtf_alignment.get(
            "label"
        )
        == "CONFLICT"
    ):
        blockers.append(
            (
                "Higher-timeframe alignment "
                "is CONFLICT"
            )
        )

    entry_grade = (
        entry_zone.get(
            "zone_grade"
        )
    )

    # Grade C is retained for calibration/reference but is too weak
    # to pass the deterministic execution gate. B remains observable
    # until the forward outcome journal provides enough evidence for
    # stricter A+/A-only gating.
    if entry_grade == "C":
        blockers.append(
            (
                "Entry Supply/Demand zone "
                "is grade C"
            )
        )

    if score < 3:
        blockers.append(
            (
                "15M setup has fewer "
                "than 3/4 signals"
            )
        )

    if not setup.get(
        "displacement"
    ):
        blockers.append(
            "15M displacement is missing"
        )

    if not setup.get(
        "structure"
    ):
        blockers.append(
            "15M BOS/CHoCH is missing"
        )

    if (
        first_rr is not None
        and first_rr < 1.0
    ):
        blockers.append(
            (
                "First liquidity target "
                f"is only {first_rr:.2f}R"
            )
        )

    higher_tf_conflict = (
        one_hour_score >= 2
        and (
            (
                direction == "long"
                and one_hour_direction
                == "bearish"
            )
            or (
                direction == "short"
                and one_hour_direction
                == "bullish"
            )
        )
    )

    if higher_tf_conflict:
        blockers.append(
            (
                "1H active setup is "
                "opposite the 15M direction"
            )
        )

    execution_ready = (
        len(blockers) == 0
    )

    if execution_ready:
        trigger = (
            "Execution map is ready. "
            "Wait for price to trade into "
            "the entry zone and show a "
            "15M hold/rejection in the "
            "setup direction."
        )
    else:
        trigger = (
            "WATCH ONLY. Do not treat "
            "the zone as an entry until "
            "the blockers clear."
        )

    return {
        "active": True,
        "execution_ready": (
            execution_ready
        ),
        "blockers": blockers,
        "higher_tf_conflict": (
            higher_tf_conflict
        ),
        "mtf_alignment": (
            mtf_alignment
        ),
        "entry_zone_grade": (
            entry_grade
        ),
        "first_target_rr": (
            first_rr
        ),
        "direction": direction,
        "setup_state": (
            setup_state
        ),
        "setup_score": score,
        "entry_zone": {
            "lower": float(
                entry_zone["lower"]
            ),
            "upper": float(
                entry_zone["upper"]
            ),
            "source": (
                entry_zone["source"]
            ),
            "zone_grade": (
                entry_zone.get(
                    "zone_grade"
                )
            ),
            "zone_quality": (
                entry_zone.get(
                    "zone_quality"
                )
            ),
        },
        "entry_mid": entry_mid,
        "stop_loss": (
            stop_loss
        ),
        "risk": risk,
        "targets": targets,
        "trigger": trigger,
    }
