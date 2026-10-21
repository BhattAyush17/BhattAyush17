#!/usr/bin/env python3
"""
finalize_snake.py

Post-process the Platane/snk contribution snake SVG.

The action output is a bare grid: no month labels, no legend, and a
progress bar (the ``u`` rects) that reads as a stray grey bar whenever
the animation is paused or rasterised.

This script:
    1. removes the progress bar rects and their CSS/keyframes
    2. adds month labels along the top
    3. adds a "Less -> More" legend
    4. tightens the viewBox to the content

It is idempotent: running it twice on the same file is safe.

Usage:
    python3 scripts/finalize_snake.py [path-to-svg]
"""

from __future__ import annotations

import calendar
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DEFAULT_SVG = Path("assets/snake/github-contribution-grid-snake-dark.svg")

# Geometry of the snk grid.
CELL_PITCH = 16
CELL_X_OFFSET = 2
GRID_TOP = 2
GRID_BOTTOM = 110  # 7 rows * 16px + 12px cell

# Overlay geometry.
MONTH_BASELINE_Y = -5
VIEWBOX_TOP = -18
LEGEND_SQUARE_Y = 123
LEGEND_BASELINE_Y = 133
VIEWBOX_BOTTOM = 142

MARKER_START = "<!-- snake-overlay-start -->"
MARKER_END = "<!-- snake-overlay-end -->"

MONTH_NAMES = [calendar.month_abbr[i] for i in range(1, 13)]

log = logging.getLogger("finalize_snake")


# ============================================================================
# REMOVAL
# ============================================================================

def strip_progress_bar(svg: str) -> str:
    """Remove the snk progress bar (``u`` rects + CSS + keyframes)."""

    # <rect class="u u0" height="12" width="518.3" x="0.0" y="144"/>
    svg = re.sub(
        r"\s*<rect class=\"u\s+[^\"]*\"[^>]*/>",
        "",
        svg,
    )

    # @keyframes u0{...{...}...{...}}
    svg = re.sub(
        r"@keyframes u\d+\{(?:[^{}]|\{[^{}]*\})*\}",
        "",
        svg,
    )

    # .u.u0{...}
    svg = re.sub(
        r"\.u\.u\d+\{[^}]*\}",
        "",
        svg,
    )

    # .u{...}
    svg = re.sub(
        r"(?<![\w.])\.u\{[^}]*\}",
        "",
        svg,
    )

    # Safety net: snk has renamed its bar classes before. Any <rect> that
    # sits below the grid and is neither a cell (.c) nor a snake part (.s)
    # is a progress bar, whatever it is called.
    def drop_if_below_grid(match: re.Match[str]) -> str:
        y = float(match.group(1))
        return "" if y > GRID_BOTTOM + 6 else match.group(0)

    svg = re.sub(
        r"\s*<rect(?![^>]*class=\"(?:c|s)[\s\"])[^>]*\sy=\"(-?[0-9.]+)\"[^>]*/>",
        drop_if_below_grid,
        svg,
    )

    return svg


def strip_overlay(svg: str) -> str:
    """Remove a previously injected overlay so the script is repeatable."""

    pattern = re.compile(
        re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
        re.S,
    )

    return pattern.sub("", svg)


# ============================================================================
# GEOMETRY
# ============================================================================

def cell_columns(svg: str) -> list[float]:
    """X positions of the contribution cells, left to right."""

    xs = {
        float(match.group(1))
        for match in re.finditer(
            r"<rect class=\"c[^\"]*\"[^>]*\sx=\"([0-9.]+)\"",
            svg,
        )
    }

    return sorted(xs)


def month_positions(
    columns: list[float],
    today: date,
) -> list[tuple[float, str]]:
    """Month label x positions, one per month present in the grid."""

    if len(columns) < 2:
        return []

    # The last column is the current week; its Sunday starts the column.
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)

    first_sunday = last_sunday - timedelta(
        days=7 * (len(columns) - 1),
    )

    positions: list[tuple[float, str]] = []
    cursor = date(first_sunday.year, first_sunday.month, 1)

    while cursor <= today:

        column = (cursor - first_sunday).days // 7

        if 0 <= column < len(columns):
            positions.append(
                (columns[column], MONTH_NAMES[cursor.month - 1])
            )

        # First day of the next month.
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)

    return positions


# ============================================================================
# OVERLAY
# ============================================================================

def build_overlay(
    columns: list[float],
    right: float,
) -> str:

    positions = month_positions(
        columns,
        datetime.now(timezone.utc).date(),
    )

    parts: list[str] = [MARKER_START]

    # ------------------------------------------------------------------
    # Month labels
    # ------------------------------------------------------------------

    if positions:

        parts.append(
            '<g class="month-labels" '
            'font-family="Segoe UI, Ubuntu, Sans-Serif" '
            'font-size="10" fill="#8b949e">'
        )

        for x, name in positions:
            parts.append(
                f'<text x="{x + 6:.1f}" y="{MONTH_BASELINE_Y}" '
                f'text-anchor="middle">{name}</text>'
            )

        parts.append("</g>")

    # ------------------------------------------------------------------
    # Less -> More legend
    # ------------------------------------------------------------------

    square = 12
    gap = 3
    squares = 5

    squares_width = squares * square + (squares - 1) * gap
    more_width = 26
    less_width = 26

    more_x = right - more_width
    squares_x = more_x - 6 - squares_width
    less_x = squares_x - 6

    parts.append(
        '<g class="snake-legend" '
        'font-family="Segoe UI, Ubuntu, Sans-Serif" '
        'font-size="10" fill="#8b949e">'
    )

    parts.append(
        f'<text x="{less_x:.1f}" y="{LEGEND_BASELINE_Y}" '
        f'text-anchor="end">Less</text>'
    )

    for index in range(squares):

        x = squares_x + index * (square + gap)

        # var(--c0) is the empty cell colour, so it needs an outline.
        parts.append(
            f'<rect x="{x:.1f}" y="{LEGEND_SQUARE_Y}" '
            f'width="{square}" height="{square}" rx="2" '
            f'fill="var(--c{index})" stroke="#30363d" '
            f'stroke-width="1" />'
        )

    parts.append(
        f'<text x="{more_x:.1f}" y="{LEGEND_BASELINE_Y}">More</text>'
    )

    parts.append("</g>")
    parts.append(MARKER_END)

    return "\n".join(parts)


def finalize(svg: str) -> str:

    svg = strip_overlay(svg)
    svg = strip_progress_bar(svg)

    # Normalise the tail so repeated runs produce identical bytes.
    svg = re.sub(r"\s*</svg>\s*$", "", svg)

    columns = cell_columns(svg)

    if not columns:
        log.warning(
            "No contribution cells found - leaving the layout untouched"
        )
        return svg + "\n</svg>\n"

    # Flush edges: the grid spans the full image width, so with
    # width="100%" its left/right edges line up exactly with the
    # 3-card row above it (each cell already has a 2px inset).
    width = columns[-1] + 12 + CELL_X_OFFSET

    overlay = build_overlay(columns, columns[-1] + 12)

    svg = svg + "\n" + overlay + "\n</svg>\n"

    # Tighten the canvas: the snk viewBox reserves space where the
    # progress bar used to be.
    svg = re.sub(
        r'viewBox="[^"]*"',
        f'viewBox="0 {VIEWBOX_TOP} {width:.0f} '
        f'{VIEWBOX_BOTTOM - VIEWBOX_TOP}"',
        svg,
        count=1,
    )

    svg = re.sub(
        r'(<svg[^>]*?)\swidth="[0-9.]+"',
        rf'\1 width="{width:.0f}"',
        svg,
        count=1,
    )

    svg = re.sub(
        r'(<svg[^>]*?)\sheight="[0-9.]+"',
        rf'\1 height="{VIEWBOX_BOTTOM - VIEWBOX_TOP}"',
        svg,
        count=1,
    )

    return svg


def main(argv: list[str]) -> int:

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        level=logging.INFO,
    )

    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_SVG

    if not path.exists():
        log.error("Missing snake SVG: %s", path)
        return 1

    svg = path.read_text(encoding="utf-8")
    finalized = finalize(svg)

    if finalized == svg:
        log.info("Snake already finalized: %s", path)
        return 0

    path.write_text(finalized, encoding="utf-8")

    log.info("Snake finalized: %s", path)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
