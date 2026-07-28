"""
Generate a sample report chart for /test command.

Creates a polished demo dashboard image with fake data
to showcase what notifications can look like.
"""

import os
import random
import tempfile
from datetime import datetime, timedelta

import numpy as np


# Brand colors
COLOR_PRIMARY = "#0073D1"
COLOR_DARK = "#1A253C"
COLOR_SUCCESS = "#10B77F"
COLOR_WARNING = "#F59F0A"
COLOR_ERROR = "#EA580C"
COLOR_BG = "#F5F7FA"
COLOR_SURFACE = "#FFFFFF"
COLOR_GRAY = "#6B7280"


def generate_test_report(username: str) -> tuple[str, str]:
    """Generate a test report image and return (image_path, caption).

    Creates a professional-looking dashboard with:
    - Revenue trend line (7 days)
    - KPI summary bar
    - Top metrics breakdown
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.patches import FancyBboxPatch

    rng = random.Random(42)

    # Generate fake data - 14 days of revenue
    today = datetime.now()
    dates = [today - timedelta(days=i) for i in range(13, -1, -1)]
    base_revenue = 52000
    revenues = [base_revenue + rng.gauss(0, 5000) + i * 300 for i, _ in enumerate(dates)]
    # Make today slightly lower for "alert" feel
    revenues[-1] = revenues[-2] * 0.82

    # KPI data
    today_rev = revenues[-1]
    yesterday_rev = revenues[-2]
    week_avg = np.mean(revenues[-7:])
    total_7d = sum(revenues[-7:])
    change_pct = ((today_rev - yesterday_rev) / yesterday_rev) * 100

    # Create figure with subplots
    fig = plt.figure(figsize=(10, 7), facecolor=COLOR_BG)
    fig.subplots_adjust(top=0.88, bottom=0.08, left=0.08, right=0.95, hspace=0.45)

    # Title
    fig.text(
        0.08,
        0.95,
        "Data Analyst Report",
        fontsize=18,
        fontweight="bold",
        color=COLOR_DARK,
        fontfamily="sans-serif",
    )
    fig.text(
        0.08,
        0.91,
        f"{today.strftime('%B %d, %Y')}  |  Demo report for {username}",
        fontsize=11,
        color=COLOR_GRAY,
        fontfamily="sans-serif",
    )

    # --- KPI Cards (top row) ---
    kpi_data = [
        ("Today's Revenue", f"${today_rev:,.0f}", f"{change_pct:+.1f}%", change_pct >= 0),
        ("7-Day Average", f"${week_avg:,.0f}", "", True),
        ("7-Day Total", f"${total_7d:,.0f}", "", True),
        ("Active Projects", f"{rng.randint(180, 220)}", "+12", True),
    ]

    for i, (label, value, badge, is_positive) in enumerate(kpi_data):
        ax_kpi = fig.add_axes([0.08 + i * 0.225, 0.72, 0.19, 0.14])
        ax_kpi.set_xlim(0, 1)
        ax_kpi.set_ylim(0, 1)
        ax_kpi.axis("off")

        # Card background
        card = FancyBboxPatch(
            (0, 0),
            1,
            1,
            boxstyle="round,pad=0.05",
            facecolor=COLOR_SURFACE,
            edgecolor="#E5E7EB",
            linewidth=1,
        )
        ax_kpi.add_patch(card)

        ax_kpi.text(0.1, 0.7, label, fontsize=8, color=COLOR_GRAY, fontfamily="sans-serif")
        ax_kpi.text(0.1, 0.25, value, fontsize=16, fontweight="bold", color=COLOR_DARK, fontfamily="sans-serif")

        if badge:
            badge_color = COLOR_SUCCESS if is_positive else COLOR_ERROR
            ax_kpi.text(
                0.9, 0.25, badge, fontsize=9, fontweight="bold", color=badge_color, ha="right", fontfamily="sans-serif"
            )

    # --- Revenue Chart ---
    ax_chart = fig.add_subplot(2, 1, 2)
    ax_chart.set_facecolor(COLOR_SURFACE)

    # Plot area fill
    ax_chart.fill_between(
        dates,
        revenues,
        alpha=0.1,
        color=COLOR_PRIMARY,
    )

    # Main line
    ax_chart.plot(
        dates[:-1],
        revenues[:-1],
        color=COLOR_PRIMARY,
        linewidth=2.5,
        solid_capstyle="round",
    )

    # Today's point (highlighted)
    ax_chart.plot(
        dates[-1],
        revenues[-1],
        "o",
        color=COLOR_ERROR,
        markersize=8,
        zorder=5,
    )
    ax_chart.plot(
        dates[-1],
        revenues[-1],
        "o",
        color=COLOR_ERROR,
        markersize=14,
        alpha=0.2,
        zorder=4,
    )

    # Dashed line connecting to today
    ax_chart.plot(
        dates[-2:],
        revenues[-2:],
        color=COLOR_ERROR,
        linewidth=2,
        linestyle="--",
        alpha=0.7,
    )

    # Average line
    ax_chart.axhline(
        y=week_avg,
        color=COLOR_WARNING,
        linewidth=1,
        linestyle=":",
        alpha=0.8,
        label=f"7d avg: ${week_avg:,.0f}",
    )

    # Styling
    ax_chart.set_title(
        "Daily Revenue (14 days)",
        fontsize=13,
        fontweight="bold",
        color=COLOR_DARK,
        loc="left",
        pad=12,
        fontfamily="sans-serif",
    )
    ax_chart.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax_chart.xaxis.set_major_locator(mdates.DayLocator(interval=2))
    plt.setp(ax_chart.xaxis.get_majorticklabels(), rotation=0, fontsize=9, color=COLOR_GRAY)
    plt.setp(ax_chart.yaxis.get_majorticklabels(), fontsize=9, color=COLOR_GRAY)

    ax_chart.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f"${x / 1000:.0f}k"))

    ax_chart.spines["top"].set_visible(False)
    ax_chart.spines["right"].set_visible(False)
    ax_chart.spines["left"].set_color("#E5E7EB")
    ax_chart.spines["bottom"].set_color("#E5E7EB")
    ax_chart.tick_params(colors="#E5E7EB")
    ax_chart.grid(axis="y", color="#F3F4F6", linewidth=0.8)

    ax_chart.legend(
        loc="upper left",
        fontsize=9,
        frameon=False,
        labelcolor=COLOR_GRAY,
    )

    # Annotate today's drop
    ax_chart.annotate(
        f"${today_rev:,.0f}\n({change_pct:+.1f}%)",
        xy=(dates[-1], revenues[-1]),
        xytext=(30, 25),
        textcoords="offset points",
        fontsize=9,
        fontweight="bold",
        color=COLOR_ERROR,
        fontfamily="sans-serif",
        arrowprops=dict(arrowstyle="->", color=COLOR_ERROR, lw=1.2),
        bbox=dict(boxstyle="round,pad=0.4", facecolor="white", edgecolor=COLOR_ERROR, alpha=0.9),
    )

    # Footer
    fig.text(
        0.5,
        0.01,
        "This is a demo report. Set up real notifications with your AI assistant.",
        fontsize=8,
        color=COLOR_GRAY,
        ha="center",
        fontstyle="italic",
        fontfamily="sans-serif",
    )

    # Save
    chart_path = os.path.join(
        tempfile.gettempdir(),
        f"notify_test_{username}_{datetime.now():%Y%m%d%H%M%S}.png",
    )
    fig.savefig(chart_path, dpi=180, bbox_inches="tight", facecolor=COLOR_BG)
    plt.close(fig)

    caption = (
        f"*Test Report for {username}*\n"
        f"Revenue today: ${today_rev:,.0f} ({change_pct:+.1f}%)\n"
        f"7d avg: ${week_avg:,.0f}\n\n"
        f"This is a demo. Ask your AI assistant to create real notifications!"
    )

    return chart_path, caption
