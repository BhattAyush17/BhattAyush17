"""
Self-Healing GitHub README Stats Generator
==========================================
Fetches SVG assets from public APIs for the monochrome README profile.

Design Philosophy:
- NEVER break the profile: if a fetch fails, keep the existing SVG
- Generate local fallback SVGs for any asset that has never existed
- Zero external dependencies beyond `requests` (no matplotlib, no bs4)
- Enforces matte black / white / silver palette on every card
"""

import os
import sys
import time
import requests

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
USERNAME = "BhattAyush17"
ASSETS_DIR = "assets/stats"
SNAKE_DIR = "assets/snake"
STREAK_DIR = "assets/streak"

# Monochrome palette tokens
BG = "121212"
TITLE = "ffffff"
TEXT = "e5e7eb"
ICON = "a1a1aa"
LINE = "a1a1aa"
POINT = "ffffff"

# ──────────────────────────────────────────────
# Asset URLs
# ──────────────────────────────────────────────
URLS = {
    # GitHub Stats card
    f"{ASSETS_DIR}/github-stats.svg": (
        f"https://github-readme-stats.vercel.app/api"
        f"?username={USERNAME}&show_icons=true"
        f"&bg_color={BG}&title_color={TITLE}"
        f"&text_color={TEXT}&icon_color={ICON}"
        f"&hide_border=true"
    ),

    # Top Languages card
    f"{ASSETS_DIR}/languages.svg": (
        f"https://github-readme-stats.vercel.app/api/top-langs/"
        f"?username={USERNAME}&layout=compact"
        f"&bg_color={BG}&title_color={TITLE}"
        f"&text_color={TEXT}&icon_color={ICON}"
        f"&hide_border=true"
    ),

    # Contribution activity graph
    f"{ASSETS_DIR}/contribution-graph.svg": (
        f"https://github-readme-activity-graph.vercel.app/graph"
        f"?username={USERNAME}&theme=github-dark"
        f"&hide_border=true&bg_color={BG}"
        f"&color={TITLE}&line={LINE}"
        f"&point={POINT}&area=true"
    ),

    # Productive-time card
    f"{ASSETS_DIR}/productive-time.svg": (
        f"https://github-profile-summary-cards.vercel.app/api/cards/productive-time"
        f"?username={USERNAME}&theme=github_dark&utcOffset=5.5"
    ),

    # GitHub Streak card
    f"{ASSETS_DIR}/streak.svg": (
        f"https://github-readme-streak-stats.herokuapp.com/"
        f"?user={USERNAME}&theme=dark"
        f"&hide_border=true&background={BG}"
        f"&ring={TITLE}&fire={TITLE}"
        f"&currStreakLabel={TEXT}&sideLabels={TEXT}"
        f"&currStreakNum={TITLE}&sideNums={TEXT}"
        f"&dates={ICON}"
    ),

    # Streak duplicate for legacy path
    f"{STREAK_DIR}/streak.svg": (
        f"https://github-readme-streak-stats.herokuapp.com/"
        f"?user={USERNAME}&theme=dark"
        f"&hide_border=true&background={BG}"
        f"&ring={TITLE}&fire={TITLE}"
        f"&currStreakLabel={TEXT}&sideLabels={TEXT}"
        f"&currStreakNum={TITLE}&sideNums={TEXT}"
        f"&dates={ICON}"
    ),

    # Contribution snake
    f"{SNAKE_DIR}/github-contribution-grid-snake-dark.svg": (
        f"https://raw.githubusercontent.com/{USERNAME}/{USERNAME}"
        f"/output/github-contribution-grid-snake-dark.svg"
    ),
}


# ──────────────────────────────────────────────
# Fallback SVG generator
# ──────────────────────────────────────────────
def generate_fallback_svg(label: str) -> str:
    """
    Creates a minimal SVG placeholder so the profile never shows a broken image.
    Matches the monochrome palette.
    """
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="495" height="195" viewBox="0 0 495 195">
  <rect width="495" height="195" rx="8" fill="#{BG}" />
  <rect x="1" y="1" width="493" height="193" rx="7" fill="none" stroke="#333" stroke-width="1"/>
  <text x="247.5" y="90" text-anchor="middle" fill="#{TEXT}" font-family="Segoe UI, sans-serif" font-size="16">
    {label}
  </text>
  <text x="247.5" y="115" text-anchor="middle" fill="#{ICON}" font-family="Segoe UI, sans-serif" font-size="11">
    Data will refresh on next CI run
  </text>
</svg>"""


# ──────────────────────────────────────────────
# Fetcher (with retry + self-healing)
# ──────────────────────────────────────────────
def fetch_and_save(filepath: str, url: str, retries: int = 3) -> bool:
    """
    Attempt to fetch an SVG from `url` and save it to `filepath`.
    If all attempts fail AND no previous file exists, write a fallback SVG.
    Returns True on success, False on failure.
    """
    filename = os.path.basename(filepath)

    for attempt in range(1, retries + 1):
        try:
            print(f"  [{attempt}/{retries}] Fetching {filename} ...")
            resp = requests.get(url, timeout=20)

            # Validate: must be 200 and contain SVG content
            if resp.status_code == 200 and "<svg" in resp.text.lower():
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(resp.text)
                print(f"  ✅ {filename} updated ({len(resp.text)} bytes)")
                return True
            else:
                print(f"  ⚠️  {filename}: HTTP {resp.status_code}, "
                      f"SVG valid={'<svg' in resp.text.lower()}")

        except requests.exceptions.Timeout:
            print(f"  ⏱️  {filename}: request timed out")
        except requests.exceptions.ConnectionError:
            print(f"  🔌 {filename}: connection failed")
        except Exception as exc:
            print(f"  ❌ {filename}: {exc}")

        if attempt < retries:
            wait = 2 ** attempt
            print(f"     Retrying in {wait}s ...")
            time.sleep(wait)

    # All retries exhausted
    if os.path.isfile(filepath) and os.path.getsize(filepath) > 0:
        print(f"  ⏭️  {filename}: keeping existing file (self-healing)")
        return False

    # No existing file → write fallback
    label = filename.replace(".svg", "").replace("-", " ").title()
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(generate_fallback_svg(label))
    print(f"  🛡️  {filename}: wrote fallback SVG")
    return False


# ──────────────────────────────────────────────
# Achievement badges (pure SVG, no scraping)
# ──────────────────────────────────────────────
def generate_achievements_card() -> None:
    """
    Generate a local monochrome achievements card SVG.
    Uses hardcoded badges since GitHub doesn't expose them via API,
    and web scraping is fragile / breaks every few months.
    """
    badges = ["Pull Shark", "Quickdraw", "YOLO"]
    filepath = f"{ASSETS_DIR}/achievements.svg"

    pills = ""
    x = 20
    for badge in badges:
        w = max(90, len(badge) * 10 + 30)
        pills += f"""
    <rect x="{x}" y="55" width="{w}" height="34" rx="17" fill="#1f1f1f" stroke="#333" stroke-width="1"/>
    <text x="{x + w // 2}" y="77" text-anchor="middle" fill="#{TEXT}" font-family="Segoe UI, sans-serif" font-size="12">
      🏆 {badge}
    </text>"""
        x += w + 14

    total_width = max(x + 20, 495)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total_width}" height="110" viewBox="0 0 {total_width} 110">
  <rect width="{total_width}" height="110" rx="8" fill="#{BG}" />
  <rect x="1" y="1" width="{total_width - 2}" height="108" rx="7" fill="none" stroke="#333" stroke-width="1"/>
  <text x="20" y="32" fill="#{TITLE}" font-family="Segoe UI, sans-serif" font-size="16" font-weight="600">
    Achievements
  </text>
  {pills}
</svg>"""

    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"  ✅ achievements.svg generated locally")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main() -> None:
    print("🚀 Self-Healing GitHub Stats Generator")
    print(f"   User: {USERNAME}")
    print(f"   Assets: {ASSETS_DIR}")
    print()

    success = 0
    total = len(URLS)

    for filepath, url in URLS.items():
        if fetch_and_save(filepath, url):
            success += 1

    print()
    generate_achievements_card()

    print()
    print(f"✨ Done: {success}/{total} assets fetched fresh")

    # Verify all expected files exist
    expected = [
        f"{ASSETS_DIR}/github-stats.svg",
        f"{ASSETS_DIR}/languages.svg",
        f"{ASSETS_DIR}/contribution-graph.svg",
        f"{ASSETS_DIR}/productive-time.svg",
        f"{ASSETS_DIR}/streak.svg",
        f"{ASSETS_DIR}/achievements.svg",
        f"{STREAK_DIR}/streak.svg",
        f"{SNAKE_DIR}/github-contribution-grid-snake-dark.svg",
    ]

    missing = [f for f in expected if not os.path.isfile(f)]
    if missing:
        print()
        print("⚠️  Missing assets (will use fallback):")
        for f in missing:
            label = os.path.basename(f).replace(".svg", "").replace("-", " ").title()
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(generate_fallback_svg(label))
            print(f"   🛡️  {f} → fallback written")

    print()
    print("🔒 Profile is failproof. No broken images possible.")


if __name__ == "__main__":
    main()
