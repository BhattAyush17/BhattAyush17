#!/usr/bin/env python3
"""
cards_svg.py

Monochrome (black / grey / white) renderers for the three profile cards:

    1. GitHub stats      -> concentric progress rings
    2. Top languages     -> donut + legend
    3. Current streak    -> flame, big number, day strip

Every card is 300x210. Besides the three standalone SVGs, ``render_combined``
lays them out side by side in ONE svg (``github-cards.svg``), so the README
can show a guaranteed 3-across row with a single <img width="100%">.

Pure functions: no network, no files. generate_stats.py does the I/O.
"""

from __future__ import annotations

import math
from html import escape
from typing import Any, Optional

CARD_W = 300
CARD_H = 210
GAP = 12
COMBINED_W = CARD_W * 3 + GAP * 2

# Palette (GitHub dark greys, no hue).
BG = "#0d1117"
LINE = "#30363d"
TRACK = "#21262d"
T1 = "#e6edf3"
T2 = "#adbac7"
T3 = "#8b949e"
DASH = "#484f58"

RING_COLORS = ("#e6edf3", "#8b949e", "#6e7681")

# Neighbouring slices alternate light / dark so they stay distinguishable.
SLICE_SHADES = ("#e6edf3", "#6e7681", "#c9d1d9", "#484f58", "#adbac7", "#8b949e")
SLICE_REST = "#30363d"
MAX_LEGEND = 6

GITHUB_PATH = (
    "M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 "
    "0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13"
    "-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66"
    ".07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15"
    "-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 "
    "1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 "
    "1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 "
    "1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"
)

FLAME_PATH = (
    "M12 12c2-2.96 0-7-1-8 0 3.038-1.773 4.741-3 6-1.226 1.26-2 3.24-2 5a6 6 "
    "0 1 0 12 0c0-1.532-1.056-3.94-2-5-1.786 3-2.791 3-4 2z"
)

STYLE = f"""<style>
.t{{font:600 15px 'Segoe UI',Ubuntu,Sans-Serif;fill:{T1}}}
.l{{font:10.5px 'Segoe UI',Ubuntu,Sans-Serif;fill:{T3}}}
.v{{font:600 14px 'Segoe UI',Ubuntu,Sans-Serif;fill:{T1}}}
.n{{font:600 20px 'Segoe UI',Ubuntu,Sans-Serif;fill:{T1}}}
.big{{font:600 44px 'Segoe UI',Ubuntu,Sans-Serif;fill:{T1}}}
.nm{{font:600 10.5px 'Segoe UI',Ubuntu,Sans-Serif;fill:{T1}}}
.pc{{font:10.5px 'Segoe UI',Ubuntu,Sans-Serif;fill:{T3}}}
.f{{font:10px 'Segoe UI',Ubuntu,Sans-Serif;fill:{T3}}}
</style>"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _num(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else escape(str(value))


def _days(value: int) -> str:
    return f"{value} day" if value == 1 else f"{value} days"


def _ellipsize(text: str, limit: int = 16) -> str:
    """Keep long language names from running into the percentage column."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _ring(cx: float, cy: float, r: float, frac: float, color: str, width: float) -> str:
    circumference = 2 * math.pi * r
    filled = circumference * _clamp01(frac)
    track = (
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{TRACK}" '
        f'stroke-width="{width}"/>'
    )
    if filled <= 0:
        return track
    arc = (
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
        f'stroke-width="{width}" stroke-linecap="round" '
        f'stroke-dasharray="{filled:.1f} {circumference:.1f}" '
        f'transform="rotate(-90 {cx} {cy})"/>'
    )
    return track + arc


def _frame(x: float, title: str, icon: str, badge: tuple[str, str], body: str) -> str:
    """One card: outline, header (icon + title + live badge), divider, body."""

    dot_color, label = badge

    return f"""<g transform="translate({x},0)">
<rect x="0.5" y="0.5" width="{CARD_W - 1}" height="{CARD_H - 1}" rx="8" fill="{BG}" stroke="{LINE}"/>
{icon}
<text x="42" y="31" class="t">{escape(title)}</text>
<circle cx="{CARD_W - 16 - 3}" cy="26" r="3" fill="{dot_color}"/>
<text x="{CARD_W - 28}" y="30" text-anchor="end" class="f">{escape(label)}</text>
<line x1="16" y1="46" x2="{CARD_W - 16}" y2="46" stroke="{LINE}"/>
{body}
</g>"""


def _github_icon() -> str:
    return (
        f'<g transform="translate(16,14)"><svg viewBox="0 0 16 16" width="20" height="20">'
        f'<path fill="{T1}" d="{GITHUB_PATH}"/></svg></g>'
    )


def _flame_icon() -> str:
    return (
        f'<g transform="translate(16,14)"><svg viewBox="0 0 24 24" width="20" height="20">'
        f'<path fill="none" stroke="{T1}" stroke-width="1.75" stroke-linecap="round" '
        f'stroke-linejoin="round" d="{FLAME_PATH}"/></svg></g>'
    )


def _footer(text: str) -> str:
    return (
        f'<line x1="16" y1="172" x2="{CARD_W - 16}" y2="172" stroke="{LINE}"/>'
        f'<text x="16" y="193" class="f">{escape(text)}</text>'
    )


# ---------------------------------------------------------------------------
# card 1: stats rings
# ---------------------------------------------------------------------------

def _stats_body(d: dict[str, Any]) -> str:
    total = max(1, int(d["total"]))
    fracs = (
        d["commits"] / total,
        d["active_days"] / 365,
        (d["current"] / d["longest"]) if d["longest"] else 0,
    )

    cx, cy = 72, 110
    rings = "".join(
        _ring(cx, cy, r, f, color, 8)
        for r, f, color in zip((46, 35, 24), fracs, RING_COLORS)
    )

    rows = (
        (RING_COLORS[0], "Commits", _num(d["commits"])),
        (RING_COLORS[1], "Active days", _num(d["active_days"])),
        (RING_COLORS[2], "Streak / best", f'{d["current"]} / {d["longest"]}'),
    )

    legend = ""
    for i, (color, label, value) in enumerate(rows):
        y = 78 + i * 32
        legend += (
            f'<circle cx="148" cy="{y - 4}" r="4" fill="{color}"/>'
            f'<text x="160" y="{y}" class="l">{label}</text>'
            f'<text x="160" y="{y + 16}" class="v">{value}</text>'
        )

    prs = int(d["prs"])
    footer = f'{prs} pull request{"" if prs == 1 else "s"}'
    if d.get("since"):
        footer += f' · since {d["since"]}'

    return (
        rings
        + f'<text x="{cx}" y="{cy + 2}" text-anchor="middle" class="n">{_num(d["total"])}</text>'
        + f'<text x="{cx}" y="{cy + 16}" text-anchor="middle" class="l">total</text>'
        + legend
        + _footer(footer)
    )


def stats_card(d: dict[str, Any], badge: tuple[str, str], x: float = 0) -> str:
    return _frame(x, "GitHub stats", _github_icon(), badge, _stats_body(d))


# ---------------------------------------------------------------------------
# card 2: languages donut
# ---------------------------------------------------------------------------

def _languages_body(d: dict[str, Any]) -> str:
    langs = sorted(d["languages"], key=lambda l: -float(l["pct"]))
    shown = langs[:MAX_LEGEND]
    hidden = langs[MAX_LEGEND:]
    hidden_pct = sum(float(l["pct"]) for l in hidden)

    cx, cy, r, width = 72, 110, 36, 18
    circumference = 2 * math.pi * r

    slices = [(float(l["pct"]), SLICE_SHADES[i % len(SLICE_SHADES)]) for i, l in enumerate(shown)]
    covered = sum(p for p, _ in slices) + hidden_pct
    if hidden_pct:
        slices.append((hidden_pct, SLICE_REST))
    if covered < 100 - 0.05:
        slices.append((100 - covered, SLICE_REST))

    arcs = ""
    cumulative = 0.0
    for pct, color in slices:
        length = pct / 100 * circumference
        arcs += (
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
            f'stroke-width="{width}" stroke-dasharray="{max(0.0, length - 1.2):.1f} {circumference:.1f}" '
            f'stroke-dashoffset="{-cumulative:.1f}" transform="rotate(-90 {cx} {cy})"/>'
        )
        cumulative += length

    legend = ""
    for i, lang in enumerate(shown):
        y = 70 + i * 18
        legend += (
            f'<circle cx="148" cy="{y - 4}" r="4" fill="{SLICE_SHADES[i % len(SLICE_SHADES)]}"/>'
            f'<text x="160" y="{y}" class="nm">{escape(_ellipsize(str(lang["name"])))}</text>'
            f'<text x="{CARD_W - 16}" y="{y}" text-anchor="end" class="pc">{float(lang["pct"]):g}%</text>'
        )

    footer = f'{d["repos"]} repositories'
    if hidden:
        footer += f' · +{len(hidden)} more'
    footer += f' · as of {d["as_of"]}'

    return (
        arcs
        + f'<text x="{cx}" y="{cy + 2}" text-anchor="middle" class="n">{_num(d["repos"])}</text>'
        + f'<text x="{cx}" y="{cy + 16}" text-anchor="middle" class="l">repos</text>'
        + legend
        + _footer(footer)
    )


def languages_card(d: dict[str, Any], badge: tuple[str, str], x: float = 0) -> str:
    return _frame(x, "Top languages", _github_icon(), badge, _languages_body(d))


# ---------------------------------------------------------------------------
# card 3: streak strip
# ---------------------------------------------------------------------------

def _streak_body(d: dict[str, Any]) -> str:
    current = int(d["current"])
    longest = max(int(d["longest"]), current, 0)

    count = min(max(longest, 1), 30)
    on = 0 if current <= 0 else max(1, round(current / longest * count))
    left, right = 16, CARD_W - 16
    gap = 2
    cell = (right - left - gap * (count - 1)) / count

    cells = ""
    for i in range(count):
        cx = left + i * (cell + gap)
        if i < on:
            cells += f'<rect x="{cx:.1f}" y="128" width="{cell:.1f}" height="14" rx="2" fill="{T1}"/>'
        else:
            cells += (
                f'<rect x="{cx + 0.5:.1f}" y="128.5" width="{cell - 1:.1f}" height="13" rx="2" '
                f'fill="none" stroke="{DASH}" stroke-dasharray="2 2"/>'
            )

    if longest <= 0:
        note = "No streak recorded yet"
    elif current >= longest:
        note = "At your record streak"
    else:
        note = f"{_days(longest - current)} to reach your record"

    return (
        f'<text x="{CARD_W / 2}" y="94" text-anchor="middle" class="big">{current}</text>'
        f'<text x="{CARD_W / 2}" y="112" text-anchor="middle" class="l">consecutive days</text>'
        + cells
        + f'<text x="16" y="158" class="f">today</text>'
        + f'<text x="{CARD_W - 16}" y="158" text-anchor="end" class="f">record: {_days(longest)}</text>'
        + _footer(note)
    )


def streak_card(d: dict[str, Any], badge: tuple[str, str], x: float = 0) -> str:
    return _frame(x, "Current streak", _flame_icon(), badge, _streak_body(d))


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------

def _wrap(width: int, content: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{CARD_H}" '
        f'viewBox="0 0 {width} {CARD_H}" role="img" aria-label="{escape(title)}">\n'
        f'<title>{escape(title)}</title>\n{STYLE}\n{content}\n</svg>\n'
    )


def render_single(kind: str, d: dict[str, Any], badge: tuple[str, str]) -> str:
    builders = {"stats": stats_card, "languages": languages_card, "streak": streak_card}
    titles = {"stats": "GitHub stats", "languages": "Top languages", "streak": "Current streak"}
    return _wrap(CARD_W, builders[kind](d, badge), titles[kind])


def render_combined(d: dict[str, Any], badge: tuple[str, str]) -> str:
    content = (
        stats_card(d, badge, 0)
        + languages_card(d, badge, CARD_W + GAP)
        + streak_card(d, badge, (CARD_W + GAP) * 2)
    )
    return _wrap(COMBINED_W, content, "GitHub stats, top languages and current streak")


# ---------------------------------------------------------------------------
# full-width card: commits by hour
# ---------------------------------------------------------------------------

def _clock_icon() -> str:
    return (
        f'<g transform="translate(16,14)"><svg viewBox="0 0 24 24" width="20" height="20" '
        f'fill="none" stroke="{T1}" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">'
        f'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg></g>'
    )


def _nice_ceiling(value: int) -> int:
    for step in (5, 10, 20, 40, 50, 100):
        if value <= step:
            return step
    return int(math.ceil(value / 50) * 50)


def render_hourly(counts: list[int], tz: str, badge: tuple[str, str]) -> str:
    """Commits per hour of day, laid out to the width of the 3-card row."""

    width = COMBINED_W
    left, right = 52, width - 20
    top, bottom = 66, 168
    plot_h = bottom - top

    peak = max(counts) if counts else 0
    ceiling = _nice_ceiling(peak)
    slot = (right - left) / 24
    bar_w = slot * 0.62

    grid = ""
    for i in range(5):
        value = ceiling * i / 4
        y = bottom - plot_h * i / 4
        grid += (
            f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{TRACK}"/>'
            f'<text x="{left - 8}" y="{y + 3.5:.1f}" text-anchor="end" class="f">{value:g}</text>'
        )

    bars = ""
    for hour, count in enumerate(counts):
        h = plot_h * count / ceiling if ceiling else 0
        x = left + hour * slot + (slot - bar_w) / 2
        color = T1 if count == peak and peak > 0 else "#6e7681"
        if h > 0:
            bars += (
                f'<rect x="{x:.1f}" y="{bottom - h:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                f'rx="2" fill="{color}"/>'
            )
        if hour % 3 == 0:
            bars += (
                f'<text x="{x + bar_w / 2:.1f}" y="{bottom + 16}" text-anchor="middle" class="f">{hour}</text>'
            )

    dot_color, label = badge
    peak_hour = counts.index(peak) if counts else 0
    note = f"Peak at {peak_hour:02d}:00 · {peak} commits" if peak else "No commit data yet"

    content = f"""<g>
<rect x="0.5" y="0.5" width="{width - 1}" height="{CARD_H - 1}" rx="8" fill="{BG}" stroke="{LINE}"/>
{_clock_icon()}
<text x="42" y="31" class="t">Commits by hour</text>
<text x="170" y="30" class="f">{escape(tz)}</text>
<circle cx="{width - 19}" cy="26" r="3" fill="{dot_color}"/>
<text x="{width - 28}" y="30" text-anchor="end" class="f">{escape(label)}</text>
<line x1="16" y1="46" x2="{width - 16}" y2="46" stroke="{LINE}"/>
{grid}{bars}
<text x="{width - 16}" y="200" text-anchor="end" class="f">{escape(note)}</text>
<text x="{left}" y="200" class="f">hour of day</text>
</g>"""
    return _wrap(width, content, f"Commits by hour of day ({tz})")
