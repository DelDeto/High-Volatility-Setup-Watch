import os

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import mplfinance as mpf
import numpy as np
import pandas as pd


# Clean dark trading palette
BG = "#0B0E11"
PANEL = "#11161D"
GRID = "#20262E"
TEXT = "#EAECEF"
MUTED = "#8A94A6"

GREEN = "#0ECB81"
RED = "#F6465D"
YELLOW = "#F0B90B"
BLUE = "#2B7FFF"
WHITE = "#D8DEE9"

SUPPLY = RED
DEMAND = GREEN
VWAP = BLUE


market_colors = mpf.make_marketcolors(
    up=GREEN,
    down=RED,
    edge={"up": GREEN, "down": RED},
    wick={"up": GREEN, "down": RED},
    volume={"up": GREEN, "down": RED},
)

chart_style = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=market_colors,
    facecolor=BG,
    figcolor=BG,
    gridcolor=GRID,
    gridstyle="-",
    y_on_right=True,
    rc={
        "axes.edgecolor": GRID,
        "axes.labelcolor": MUTED,
        "axes.titlecolor": TEXT,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "text.color": TEXT,
        "font.size": 9,
    },
)


def _fmt(value):
    if value is None:
        return "-"
    return f"{float(value):.3f}"


def _style_axis(ax):
    ax.set_facecolor(BG)

    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.tick_params(
        colors=MUTED,
        labelsize=8,
        length=0,
    )

    ax.grid(
        True,
        color=GRID,
        linewidth=0.55,
        alpha=0.58,
    )


def _price_tag(
    ax,
    price,
    last_open,
):
    color = (
        GREEN
        if price >= last_open
        else RED
    )

    ax.axhline(
        price,
        color=color,
        linewidth=0.8,
        linestyle=(0, (2, 2)),
        alpha=0.8,
        zorder=2,
    )

    ax.text(
        1.002,
        price,
        f" {_fmt(price)} ",
        transform=ax.get_yaxis_transform(),
        ha="left",
        va="center",
        fontsize=8,
        color=BG,
        clip_on=False,
        bbox=dict(
            boxstyle="square,pad=0.22",
            facecolor=color,
            edgecolor=color,
            linewidth=0,
        ),
        zorder=10,
    )


def _anchored_vwap(
    plot_df,
    analysis,
):
    setup = (
        analysis.get(
            "setup",
            {},
        )
        if analysis
        else {}
    )

    anchor_candidates = []

    for field in [
        "sweep",
        "structure",
        "displacement",
    ]:
        event = setup.get(field)

        if (
            event
            and event.get("time")
        ):
            try:
                ts = pd.Timestamp(
                    event["time"]
                )

                if ts in plot_df.index:
                    anchor_candidates.append(
                        plot_df.index.get_loc(
                            ts
                        )
                    )
            except Exception:
                pass

    if anchor_candidates:
        anchor = min(
            anchor_candidates
        )
    else:
        anchor = max(
            0,
            len(plot_df) - 96,
        )

    typical = (
        plot_df["High"]
        + plot_df["Low"]
        + plot_df["Close"]
    ) / 3

    volume = (
        plot_df["Volume"]
        .astype(float)
        .fillna(0)
    )

    vwap = pd.Series(
        np.nan,
        index=plot_df.index,
        dtype=float,
    )

    segment = slice(
        anchor,
        None,
    )

    pv = (
        typical.iloc[segment]
        * volume.iloc[segment]
    ).cumsum()

    vv = (
        volume.iloc[segment]
        .cumsum()
        .replace(
            0,
            np.nan,
        )
    )

    vwap.iloc[segment] = (
        pv / vv
    )

    return (
        vwap,
        anchor,
    )


def _zone_candidates(
    analysis,
    side,
    current_price,
    max_zones=2,
):
    """
    Active V3 zones arrive from the engine in trading priority order:
    Primary = nearest valid zone.
    Secondary = highest-quality remaining valid zone.
    """

    key = (
        "supply_zones"
        if side == "supply"
        else "demand_zones"
    )

    zones = [
        dict(zone)
        for zone in analysis.get(
            key,
            [],
        )
        if (
            zone
            and zone.get(
                "qualified",
                False,
            )
            and not zone.get(
                "invalidated",
                False,
            )
        )
    ]

    return zones[:max_zones]


def _reference_zone_candidate(
    analysis,
    side,
):
    key = (
        "reference_supply_zones"
        if side == "supply"
        else "reference_demand_zones"
    )

    zones = analysis.get(
        key,
        [],
    )

    return (
        dict(zones[0])
        if zones
        else None
    )


def _draw_zone(
    ax,
    zone,
    side,
    number,
    x_left,
    x_right,
):
    if not zone:
        return

    lower = float(
        zone["lower"]
    )

    upper = float(
        zone["upper"]
    )

    color = (
        SUPPLY
        if side == "supply"
        else DEMAND
    )

    ax.fill_between(
        [x_left, x_right],
        lower,
        upper,
        color=color,
        alpha=0.11,
        zorder=0,
    )

    ax.hlines(
        [lower, upper],
        xmin=x_left,
        xmax=x_right,
        color=color,
        linewidth=0.8,
        alpha=0.80,
        zorder=2,
    )

    label = (
        "Supply"
        if side == "supply"
        else "Demand"
    )

    tier = (
        "Primary"
        if number == 1
        else "Secondary"
    )

    quality = zone.get(
        "quality",
        "N/A",
    )

    grade = zone.get(
        "grade",
        "NA",
    )

    pattern = zone.get(
        "pattern"
    )

    mitigations = zone.get(
        "mitigations",
        0,
    )

    pattern_text = (
        f" · {pattern}"
        if pattern
        else ""
    )

    ax.text(
        x_right - 1.0,
        (
            lower
            + upper
        )
        / 2,
        (
            f"{tier} {label} · {quality} · {grade}"
            f"{pattern_text}\n"
            f"{_fmt(lower)} – {_fmt(upper)}"
            f" · M{mitigations}"
        ),
        ha="right",
        va="center",
        fontsize=7.4,
        color=(
            "#FF8A97"
            if side == "supply"
            else "#5AF0B0"
        ),
        fontweight="bold",
        bbox=dict(
            boxstyle="round,pad=0.25",
            facecolor=BG,
            edgecolor=color,
            linewidth=0.5,
            alpha=0.72,
        ),
        zorder=7,
    )


def _draw_reference_zone(
    ax,
    zone,
    side,
    x_left,
    x_right,
):
    """
    Historical zone for context only.
    It is deliberately faint/dashed and must never be interpreted
    as an active execution zone.
    """

    if not zone:
        return

    lower = float(
        zone["lower"]
    )

    upper = float(
        zone["upper"]
    )

    color = (
        SUPPLY
        if side == "supply"
        else DEMAND
    )

    ax.fill_between(
        [x_left, x_right],
        lower,
        upper,
        color=color,
        alpha=0.025,
        zorder=0,
    )

    ax.hlines(
        [lower, upper],
        xmin=x_left,
        xmax=x_right,
        color=color,
        linewidth=0.75,
        linestyle=(0, (5, 5)),
        alpha=0.45,
        zorder=1,
    )

    label = (
        "Supply"
        if side == "supply"
        else "Demand"
    )

    pattern = zone.get(
        "pattern",
        "?",
    )

    mitigations = zone.get(
        "mitigations",
        0,
    )

    ax.text(
        x_right - 1.0,
        (
            lower
            + upper
        )
        / 2,
        (
            f"Historical {label} · WEAK · {pattern}\n"
            f"{_fmt(lower)} – {_fmt(upper)}"
            f" · M{mitigations} · REFERENCE ONLY"
        ),
        ha="right",
        va="center",
        fontsize=7.0,
        color=(
            "#8DCFB1"
            if side == "demand"
            else "#C58C94"
        ),
        fontweight="normal",
        bbox=dict(
            boxstyle="round,pad=0.22",
            facecolor=BG,
            edgecolor=color,
            linewidth=0.45,
            alpha=0.55,
        ),
        zorder=5,
    )


def _draw_liquidity(
    ax,
    analysis,
    x_start,
    x_end,
):
    for key, label in [
        ("bsl", "BSL"),
        ("ssl", "SSL"),
    ]:
        point = analysis.get(key)

        if not point:
            continue

        price = float(
            point["price"]
        )

        ax.hlines(
            price,
            xmin=x_start,
            xmax=x_end,
            color=WHITE,
            linewidth=0.8,
            linestyle=(0, (4, 4)),
            alpha=0.72,
            zorder=3,
        )

        ax.text(
            x_start,
            price,
            (
                f" {label} "
                f"({_fmt(price)}) "
            ),
            ha="left",
            va=(
                "bottom"
                if key == "bsl"
                else "top"
            ),
            fontsize=6.8,
            color=WHITE,
            zorder=7,
        )


def _draw_vwap(
    ax,
    plot_df,
    analysis,
):
    vwap, anchor = (
        _anchored_vwap(
            plot_df,
            analysis,
        )
    )

    x = np.arange(
        len(plot_df)
    )

    valid = (
        ~vwap.isna()
    ).to_numpy()

    if valid.any():
        ax.plot(
            x[valid],
            vwap.to_numpy()[valid],
            color=VWAP,
            linewidth=1.35,
            alpha=0.96,
            zorder=4,
        )

        last_valid = np.where(
            valid
        )[0][-1]

        ax.text(
            last_valid,
            float(
                vwap.iloc[
                    last_valid
                ]
            ),
            " VWAP ",
            ha="left",
            va="bottom",
            fontsize=7,
            color=WHITE,
            bbox=dict(
                boxstyle="round,pad=0.18",
                facecolor="#153C72",
                edgecolor=VWAP,
                linewidth=0.6,
                alpha=0.94,
            ),
            zorder=8,
        )

    current_price = float(
        plot_df.iloc[-1][
            "Close"
        ]
    )

    current_vwap = (
        float(vwap.dropna().iloc[-1])
        if not vwap.dropna().empty
        else None
    )

    relation = "N/A"

    if current_vwap is not None:
        relation = (
            "ABOVE"
            if current_price
            >= current_vwap
            else "BELOW"
        )

    return {
        "value": current_vwap,
        "relation": relation,
        "anchor_index": anchor,
    }


def _draw_trade_targets(
    ax,
    trade_plan,
    x_now,
    x_future,
):
    if (
        not trade_plan
        or not trade_plan.get(
            "active",
            False,
        )
    ):
        return

    execution_ready = (
        trade_plan.get(
            "execution_ready",
            False,
        )
    )

    direction = trade_plan[
        "direction"
    ]

    entry = trade_plan[
        "entry_zone"
    ]

    entry_lower = float(
        entry["lower"]
    )

    entry_upper = float(
        entry["upper"]
    )

    stop = float(
        trade_plan[
            "stop_loss"
        ]
    )

    zone_color = (
        GREEN
        if execution_ready
        else YELLOW
    )

    ax.fill_between(
        [x_now - 8, x_future],
        entry_lower,
        entry_upper,
        color=zone_color,
        alpha=0.07,
        zorder=1,
    )

    ax.text(
        x_future - 0.5,
        (
            entry_lower
            + entry_upper
        ) / 2,
        (
            f"{'ENTRY' if execution_ready else 'WATCH'} "
            f"{_fmt(entry_lower)}–{_fmt(entry_upper)}"
        ),
        ha="right",
        va="center",
        fontsize=7.1,
        color=zone_color,
        bbox=dict(
            boxstyle="round,pad=0.22",
            facecolor=BG,
            edgecolor=zone_color,
            linewidth=0.7,
            alpha=0.92,
        ),
        zorder=9,
    )

    ax.hlines(
        stop,
        xmin=max(
            0,
            x_now - 25,
        ),
        xmax=x_future,
        color=RED,
        linewidth=0.9,
        linestyle=(0, (5, 4)),
        alpha=0.9,
        zorder=3,
    )

    ax.text(
        x_future,
        stop,
        (
            f" Invalidation "
            f"{_fmt(stop)} "
        ),
        ha="right",
        va=(
            "top"
            if direction == "long"
            else "bottom"
        ),
        fontsize=7.0,
        color=RED,
        bbox=dict(
            boxstyle="round,pad=0.18",
            facecolor=BG,
            edgecolor=RED,
            linewidth=0.6,
            alpha=0.9,
        ),
        zorder=9,
    )

    for index, target in enumerate(
        trade_plan.get(
            "targets",
            [],
        ),
        start=1,
    ):
        price = float(
            target["price"]
        )

        ax.hlines(
            price,
            xmin=x_now + 2,
            xmax=x_future,
            color=GREEN,
            linewidth=0.85,
            linestyle=(0, (6, 4)),
            alpha=0.86,
            zorder=3,
        )

        ax.text(
            x_future,
            price,
            (
                f" TP{index} "
                f"{_fmt(price)} "
            ),
            ha="right",
            va="bottom",
            fontsize=7.2,
            color=BG,
            bbox=dict(
                boxstyle="round,pad=0.18",
                facecolor=GREEN,
                edgecolor=GREEN,
                linewidth=0.5,
                alpha=0.95,
            ),
            zorder=9,
        )


def _scenario_points(
    current_price,
    trade_plan,
    x_now,
):
    if (
        not trade_plan
        or not trade_plan.get(
            "active",
            False,
        )
    ):
        return None

    direction = trade_plan[
        "direction"
    ]

    entry = trade_plan[
        "entry_zone"
    ]

    entry_mid = (
        float(
            entry["lower"]
        )
        + float(
            entry["upper"]
        )
    ) / 2

    targets = [
        float(item["price"])
        for item
        in trade_plan.get(
            "targets",
            [],
        )
    ]

    xs = [
        x_now,
        x_now + 3,
    ]

    ys = [
        current_price,
        entry_mid,
    ]

    for i, target in enumerate(
        targets[:3],
        start=1,
    ):
        xs.append(
            x_now + 7 * i
        )

        ys.append(target)

        if i < len(
            targets[:3]
        ):
            xs.append(
                x_now
                + 7 * i
                + 2
            )

            if direction == "long":
                ys.append(
                    target
                    - abs(
                        target
                        - entry_mid
                    )
                    * 0.12
                )
            else:
                ys.append(
                    target
                    + abs(
                        target
                        - entry_mid
                    )
                    * 0.12
                )

    return (
        np.array(xs),
        np.array(ys),
    )


def _draw_preferred_scenario(
    ax,
    current_price,
    trade_plan,
    x_now,
):
    points = _scenario_points(
        current_price,
        trade_plan,
        x_now,
    )

    if points is None:
        ax.text(
            0.69,
            0.11,
            (
                "Preferred scenario\n"
                "WAIT – no active execution map"
            ),
            transform=ax.transAxes,
            ha="left",
            va="bottom",
            fontsize=7.8,
            color=MUTED,
            bbox=dict(
                boxstyle="round,pad=0.34",
                facecolor=PANEL,
                edgecolor=GRID,
                linewidth=0.7,
                alpha=0.93,
            ),
            zorder=10,
        )

        return

    xs, ys = points

    dense_x = np.linspace(
        xs.min(),
        xs.max(),
        180,
    )

    dense_y = np.interp(
        dense_x,
        xs,
        ys,
    )

    direction = trade_plan[
        "direction"
    ]

    scenario_color = (
        GREEN
        if direction == "long"
        else RED
    )

    ax.plot(
        dense_x,
        dense_y,
        color=scenario_color,
        linewidth=2.0,
        alpha=0.95,
        zorder=6,
    )

    ax.annotate(
        "",
        xy=(
            dense_x[-1],
            dense_y[-1],
        ),
        xytext=(
            dense_x[-12],
            dense_y[-12],
        ),
        arrowprops=dict(
            arrowstyle="-|>",
            color=scenario_color,
            lw=2.0,
            mutation_scale=18,
        ),
        zorder=7,
    )

    execution_ready = (
        trade_plan.get(
            "execution_ready",
            False,
        )
    )

    if direction == "long":
        scenario = (
            "Preferred scenario:\n"
            "pullback into demand/FVG →\n"
            "hold above VWAP → bullish continuation"
        )
    else:
        scenario = (
            "Preferred scenario:\n"
            "retest supply/FVG →\n"
            "hold below VWAP → bearish continuation"
        )

    if not execution_ready:
        scenario += (
            "\nExecution: WAIT"
        )

    ax.text(
        min(
            xs.max() - 12,
            x_now + 4,
        ),
        np.mean(
            [
                current_price,
                ys[-1],
            ]
        ),
        scenario,
        ha="left",
        va="center",
        fontsize=7.5,
        color=scenario_color,
        bbox=dict(
            boxstyle="round,pad=0.34",
            facecolor=PANEL,
            edgecolor=scenario_color,
            linewidth=0.7,
            alpha=0.94,
        ),
        zorder=9,
    )


def _draw_side_panel(
    fig,
    analysis,
    trade_plan,
    vwap_info,
    market_snapshot=None,
):
    setup = analysis.get(
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

    direction = (
        trade_plan.get(
            "direction"
        )
        if trade_plan
        else None
    )

    if direction:
        bias = direction.upper()
    else:
        bias = analysis.get(
            "trend",
            "mixed",
        ).upper()

    ready = bool(
        trade_plan
        and trade_plan.get(
            "execution_ready",
            False,
        )
    )

    regime = analysis.get(
        "regime",
        "N/A",
    )

    alignment = (
        trade_plan.get(
            "mtf_alignment",
            {},
        ).get(
            "label",
            "N/A",
        )
        if trade_plan
        else "N/A"
    )

    vwap_relation = (
        vwap_info.get(
            "relation",
            "N/A",
        )
    )

    vwap_align = (
        (
            direction == "long"
            and vwap_relation
            == "ABOVE"
        )
        or (
            direction == "short"
            and vwap_relation
            == "BELOW"
        )
    )

    confluence = min(
        5,
        score
        + (
            1
            if vwap_align
            else 0
        ),
    )

    hold_change = None
    funding = None
    participation = "N/A"

    if market_snapshot:
        hold_change = (
            market_snapshot.get(
                "hold_vol_change_pct"
            )
        )

        funding = (
            market_snapshot.get(
                "funding_rate"
            )
        )

        participation = (
            market_snapshot.get(
                "participation_context",
                {},
            ).get(
                "regime",
                "N/A",
            )
        )

    if hold_change is None:
        oi_text = "N/A"
    elif hold_change > 0.05:
        oi_text = (
            f"UP {hold_change:+.2f}%"
        )
    elif hold_change < -0.05:
        oi_text = (
            f"DOWN {hold_change:+.2f}%"
        )
    else:
        oi_text = (
            f"FLAT {hold_change:+.2f}%"
        )

    funding_text = (
        f"{funding * 100:+.4f}%"
        if funding is not None
        else "N/A"
    )

    panel_text = (
        f"Bias: {bias}\n"
        f"Regime: {regime}\n"
        f"MTF: {alignment}\n"
        f"Execution: "
        f"{'READY' if ready else 'WAIT'}\n"
        f"HoldVol: {oi_text}\n"
        f"Participation: {participation}\n"
        f"Funding: {funding_text}\n"
        f"VWAP: {vwap_relation}\n"
        f"Confluence: {confluence}/5"
    )

    fig.text(
        0.842,
        0.885,
        panel_text,
        ha="left",
        va="top",
        fontsize=9.0,
        color=TEXT,
        linespacing=1.55,
        bbox=dict(
            boxstyle="round,pad=0.70",
            facecolor=PANEL,
            edgecolor=GRID,
            linewidth=0.9,
            alpha=0.97,
        ),
    )

def create_chart(
    df,
    interval,
    output_path,
    candles=100,
    exchange="MEXC",
    analysis=None,
    trade_plan=None,
    market_snapshot=None,
    symbol="VVV/USDT",
):
    """
    Clean scenario chart:
    candles + Supply/Demand + liquidity + VWAP + preferred scenario.
    Volume panel is intentionally removed.
    """

    output_dir = os.path.dirname(
        output_path
    )

    if output_dir:
        os.makedirs(
            output_dir,
            exist_ok=True,
        )

    plot_df = df[
        [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
    ].copy()

    plot_df.columns = [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    ]

    plot_df = (
        plot_df
        .tail(candles)
        .copy()
    )

    current_price = float(
        plot_df.iloc[-1][
            "Close"
        ]
    )

    last_open = float(
        plot_df.iloc[-1][
            "Open"
        ]
    )

    last_high = float(
        plot_df.iloc[-1][
            "High"
        ]
    )

    last_low = float(
        plot_df.iloc[-1][
            "Low"
        ]
    )

    fig, axes = mpf.plot(
        plot_df,
        type="candle",
        volume=False,
        style=chart_style,
        ylabel="",
        datetime_format="%m-%d %H:%M",
        xrotation=0,
        figsize=(16, 9),
        returnfig=True,
        tight_layout=False,
        update_width_config={
            "candle_width": 0.64,
            "candle_linewidth": 0.72,
        },
    )

    fig.patch.set_facecolor(
        BG
    )

    ax = axes[0]

    _style_axis(ax)

    fig.subplots_adjust(
        left=0.055,
        right=0.82,
        top=0.88,
        bottom=0.105,
    )

    x_now = (
        len(plot_df) - 1
    )

    x_future = (
        x_now + 24
    )

    ax.set_xlim(
        -1,
        x_future + 2,
    )

    # Header
    fig.text(
        0.058,
        0.955,
        (
            f"{symbol.replace('_', '/')}   {interval}"
        ),
        ha="left",
        va="top",
        fontsize=17,
        fontweight="bold",
        color=TEXT,
    )

    header_price = (
        market_snapshot.get(
            "last_price"
        )
        if market_snapshot
        else None
    )

    if header_price is None:
        header_price = current_price

    fig.text(
        0.23,
        0.955,
        _fmt(header_price),
        ha="left",
        va="top",
        fontsize=18,
        fontweight="bold",
        color=(
            GREEN
            if header_price
            >= last_open
            else RED
        ),
    )

    header_bits = [
        f"O {_fmt(last_open)}",
        f"H {_fmt(last_high)}",
        f"L {_fmt(last_low)}",
        f"C {_fmt(current_price)}",
        f"· {exchange}",
    ]

    if market_snapshot:
        high_24h = (
            market_snapshot.get(
                "high_24h"
            )
        )

        low_24h = (
            market_snapshot.get(
                "low_24h"
            )
        )

        change_24h = (
            market_snapshot.get(
                "change_rate_24h"
            )
        )

        if high_24h is not None:
            header_bits.append(
                f"24H H {_fmt(high_24h)}"
            )

        if low_24h is not None:
            header_bits.append(
                f"24H L {_fmt(low_24h)}"
            )

        if change_24h is not None:
            header_bits.append(
                (
                    "24H "
                    f"{change_24h * 100:+.2f}%"
                )
            )

    fig.text(
        0.058,
        0.925,
        "   ".join(
            header_bits
        ),
        ha="left",
        va="top",
        fontsize=8.7,
        color=MUTED,
    )

    _price_tag(
        ax,
        current_price,
        last_open,
    )

    if analysis:
        # Draw up to two relevant zones on each side.
        supplies = _zone_candidates(
            analysis,
            "supply",
            current_price,
            max_zones=2,
        )

        demands = _zone_candidates(
            analysis,
            "demand",
            current_price,
            max_zones=2,
        )

        for i, zone in enumerate(
            supplies,
            start=1,
        ):
            _draw_zone(
                ax,
                zone,
                "supply",
                i,
                max(
                    0,
                    x_now - 68,
                ),
                x_future - 2,
            )

        for i, zone in enumerate(
            demands,
            start=1,
        ):
            _draw_zone(
                ax,
                zone,
                "demand",
                i,
                max(
                    0,
                    x_now - 68,
                ),
                x_future - 2,
            )

        # If no active zone exists on a side, show the nearest historical
        # over-mitigated zone as context only. It is never used by Trade Map.
        if not supplies:
            reference_supply = (
                _reference_zone_candidate(
                    analysis,
                    "supply",
                )
            )

            _draw_reference_zone(
                ax,
                reference_supply,
                "supply",
                max(
                    0,
                    x_now - 52,
                ),
                x_future - 2,
            )

        if not demands:
            reference_demand = (
                _reference_zone_candidate(
                    analysis,
                    "demand",
                )
            )

            _draw_reference_zone(
                ax,
                reference_demand,
                "demand",
                max(
                    0,
                    x_now - 52,
                ),
                x_future - 2,
            )

        _draw_liquidity(
            ax,
            analysis,
            max(
                0,
                x_now - 40,
            ),
            min(
                x_future - 5,
                x_now + 6,
            ),
        )

        vwap_info = _draw_vwap(
            ax,
            plot_df,
            analysis,
        )

        # Only the execution timeframe receives the projected trade map.
        if trade_plan is not None:
            _draw_trade_targets(
                ax,
                trade_plan,
                x_now,
                x_future,
            )

            _draw_preferred_scenario(
                ax,
                current_price,
                trade_plan,
                x_now,
            )

        _draw_side_panel(
            fig,
            analysis,
            trade_plan,
            vwap_info,
            market_snapshot=(
                market_snapshot
            ),
        )

    fig.savefig(
        output_path,
        dpi=165,
        facecolor=BG,
        bbox_inches="tight",
    )

    plt.close(fig)

    print(
        f"Saved chart: {output_path}"
    )



def render_setup_chart(symbol, frame_15m, item):
    output_path = os.path.join(
        "output",
        "charts",
        f"{symbol.replace('/', '_').replace(':', '_')}_15M.png",
    )

    create_chart(
        frame_15m,
        "15M",
        output_path,
        candles=160,
        exchange="MEXC",
        analysis=item.get("analysis_15m"),
        trade_plan=item.get("trade_plan"),
        market_snapshot=item.get("ticker") or {},
        symbol=symbol,
    )

    return output_path
