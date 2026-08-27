"""
generate_stats.py — Robust, failsafe GitHub README stats generator.
Strategy:
  1. Fetch live data via GitHub GraphQL API (with retries + exponential back-off).
  2. On ANY failure, load from a local cache (assets/cache/data.json) if present.
  3. Render all SVG cards using Matplotlib — fully offline-resilient.
  4. On card-render failure, copy a pre-baked SVG fallback from assets/fallback/.
  5. Write back the freshly fetched data to the cache for future fallbacks.
"""

import os, sys, re, json, time, shutil, hashlib, requests, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── Matplotlib ────────────────────────────────────────────────────────────────
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from bs4 import BeautifulSoup

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("stats")

# ─── Paths & constants ─────────────────────────────────────────────────────────
USERNAME     = "BhattAyush17"
ASSETS_DIR   = Path("assets/stats")
BADGES_DIR   = Path("assets/badges")
SNAKE_DIR    = Path("assets/snake")
FALLBACK_DIR = Path("assets/fallback")
CACHE_DIR    = Path("assets/cache")
CACHE_FILE   = CACHE_DIR / "data.json"
CACHE_MAX_AGE = timedelta(hours=6)           # fresh-enough threshold

TOKEN        = os.getenv("GH_STATS_TOKEN", "")
GRAPHQL_URL  = "https://api.github.com/graphql"
REST_BASE    = "https://api.github.com"

HEADERS = {"Content-Type": "application/json"}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"
else:
    log.warning("GH_STATS_TOKEN is not set — API calls may be rate-limited or fail.")

# Colour palette (dark-theme)
BG, CARD, GRID = "#121212", "#121212", "#27272a"
TEXT, ACCENT, MUTED = "#e5e7eb", "#3b82f6", "#9ca3af"
COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"]

# ─── Retry helper ──────────────────────────────────────────────────────────────
def _get(url: str, *, timeout: int = 20, retries: int = 3, **kwargs) -> requests.Response:
    """GET with exponential back-off, raising on persistent failure."""
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=timeout, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            wait = 2 ** attempt
            log.warning("GET %s failed (attempt %d/%d): %s — retrying in %ds", url, attempt, retries, exc, wait)
            if attempt < retries:
                time.sleep(wait)
    raise RuntimeError(f"All {retries} attempts failed for {url}")


def _post(url: str, *, timeout: int = 20, retries: int = 3, **kwargs) -> requests.Response:
    """POST with exponential back-off."""
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, timeout=timeout, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            wait = 2 ** attempt
            log.warning("POST %s failed (attempt %d/%d): %s — retrying in %ds", url, attempt, retries, exc, wait)
            if attempt < retries:
                time.sleep(wait)
    raise RuntimeError(f"All {retries} attempts failed for {url}")


# ─── Cache helpers ─────────────────────────────────────────────────────────────
def load_cache() -> dict | None:
    """Return cached data if it exists and is fresh enough."""
    if not CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(CACHE_FILE.read_text("utf-8"))
        ts = datetime.fromisoformat(payload.get("_cached_at", "1970-01-01T00:00:00+00:00"))
        age = datetime.now(timezone.utc) - ts
        if age <= CACHE_MAX_AGE:
            log.info("Cache hit — age %s (≤ %s).", age, CACHE_MAX_AGE)
        else:
            log.info("Cache stale — age %s (> %s), but usable as emergency fallback.", age, CACHE_MAX_AGE)
        return payload.get("data")
    except Exception as exc:
        log.warning("Cache read failed: %s", exc)
        return None


def save_cache(data: dict) -> None:
    """Persist freshly-fetched API data with a timestamp."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"_cached_at": datetime.now(timezone.utc).isoformat(), "data": data}
    try:
        CACHE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.info("Cache written → %s", CACHE_FILE)
    except Exception as exc:
        log.warning("Cache write failed: %s", exc)


# ─── GitHub API ────────────────────────────────────────────────────────────────
_GQL_QUERY = """
query($login: String!) {
  user(login: $login) {
    name
    bio
    location
    repositories(first: 100, ownerAffiliations: OWNER, privacy: PUBLIC, orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      nodes {
        name
        stargazerCount
        forkCount
        primaryLanguage { name color }
        pushedAt
        description
        url
      }
    }
    contributionsCollection {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            contributionCount
            date
          }
        }
      }
    }
    followers  { totalCount }
    following  { totalCount }
  }
}
"""


def fetch_live_data() -> dict:
    """
    Fetch live data from GitHub GraphQL API.
    Raises on failure so caller can fall back to cache.
    """
    if not TOKEN:
        raise RuntimeError("No GH_STATS_TOKEN — cannot call authenticated GraphQL endpoint.")

    r = _post(
        GRAPHQL_URL,
        headers=HEADERS,
        json={"query": _GQL_QUERY, "variables": {"login": USERNAME}},
    )
    payload = r.json()

    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    if not payload.get("data", {}).get("user"):
        raise RuntimeError(f"GraphQL returned no user data: {payload}")

    return payload["data"]


def get_github_data() -> dict:
    """
    Fetch live data; fall back to cache on ANY error.
    If both fail, raise so the workflow can surface a useful message.
    """
    try:
        data = fetch_live_data()
        save_cache(data)
        log.info("✅ Live GitHub data fetched and cached.")
        return data
    except Exception as live_exc:
        log.warning("Live fetch failed: %s — trying cache…", live_exc)

    cached = load_cache()
    if cached:
        log.info("Using cached data as fallback.")
        return cached

    raise RuntimeError(
        "Both live API fetch and local cache failed. "
        "Set GH_STATS_TOKEN and ensure assets/cache/data.json exists after first run."
    )


# ─── Streak helper ─────────────────────────────────────────────────────────────
def calculate_streak(weeks: list) -> tuple[int, int]:
    today = datetime.now(timezone.utc).date().isoformat()
    all_days = [d for w in weeks for d in w["contributionDays"] if d["date"] <= today]

    current = 0
    for day in reversed(all_days):
        if day["contributionCount"] > 0:
            current += 1
        elif day["date"] == today:
            continue          # today may not have any commits yet
        else:
            break

    longest, running = 0, 0
    for day in all_days:
        if day["contributionCount"] > 0:
            running += 1
            longest = max(longest, running)
        else:
            running = 0

    return current, longest


# ─── SVG fallback helper ───────────────────────────────────────────────────────
def _fallback_copy(name: str, dest: Path) -> None:
    """Copy a pre-baked fallback SVG if it exists; otherwise generate a minimal placeholder MARKED as unavailable."""
    src = FALLBACK_DIR / name
    if src.exists():
        shutil.copy(src, dest)
        log.info("Fallback copied: %s → %s", src, dest)
    else:
        # Mark this SVG so the health check knows to retry, not treat as healthy
        _write_minimal_svg_with_marker(dest, f"Data unavailable — will retry", placeholder=True)


def _write_minimal_svg(path: Path, label: str) -> None:
    """Write a minimal dark placeholder SVG (for harmless cases like no language data)."""
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120" viewBox="0 0 400 120">'
        f'<rect width="400" height="120" rx="8" fill="#121212" stroke="#27272a" stroke-width="1"/>'
        f'<text x="200" y="66" text-anchor="middle" fill="#9ca3af" font-family="sans-serif" font-size="14">{label}</text>'
        f"</svg>",
        encoding="utf-8",
    )
    log.info("Placeholder written: %s", path)


def _write_minimal_svg_with_marker(path: Path, label: str, placeholder: bool = False) -> None:
    """Write a minimal SVG with an internal marker so health checks can distinguish temporary failures from real data."""
    marker = '<!-- PLACEHOLDER: WILL_RETRY -->' if placeholder else ''
    path.write_text(
        f'{marker}'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120" viewBox="0 0 400 120">'
        f'<rect width="400" height="120" rx="8" fill="#121212" stroke="#27272a" stroke-width="1"/>'
        f'<text x="200" y="66" text-anchor="middle" fill="#9ca3af" font-family="sans-serif" font-size="14">{label}</text>'
        f"</svg>",
        encoding="utf-8",
    )
    log.info("Placeholder with retry marker written: %s", path)


def _save_svg(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format="svg", transparent=True, bbox_inches="tight", pad_inches=0)
    plt.close(fig)


# ─── GitHub logo ───────────────────────────────────────────────────────────────
def _get_github_logo() -> Path | None:
    logo = ASSETS_DIR / "github-mark.png"
    if logo.exists():
        return logo
    try:
        r = _get("https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png", retries=2)
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        logo.write_bytes(r.content)
        log.info("✅ GitHub logo downloaded.")
        return logo
    except Exception as exc:
        log.warning("Could not download GitHub logo: %s", exc)
        return None


# ─── Card renderers ────────────────────────────────────────────────────────────
def _rounded_box(ax, fig=None) -> FancyBboxPatch:
    box = FancyBboxPatch(
        (0.02, 0.02), 0.96, 0.96,
        boxstyle="round,pad=0.01",
        edgecolor=GRID, facecolor=CARD, linewidth=1.5,
        transform=ax.transAxes if fig is None else fig.transFigure,
    )
    return box


def make_stats_svg(data: dict) -> None:
    out = ASSETS_DIR / "github-stats.svg"
    try:
        user  = data["user"]
        repos = user["repositories"]["nodes"]
        stars = sum(r["stargazerCount"] for r in repos)
        forks = sum(r["forkCount"] for r in repos)
        cc    = user["contributionsCollection"]
        commits = cc["totalCommitContributions"] + cc.get("restrictedContributionsCount", 0)
        prs = cc["totalPullRequestContributions"]
        issues = cc["totalIssueContributions"]
        followers = user["followers"]["totalCount"]

        log.info("Rendering stats with real data: %d stars, %d forks, %d commits", stars, forks, commits)

        svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" width="450" height="195" viewBox="0 0 450 195">
  <style>
    .title {{ font: bold 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: #e6edf3; }}
    .label {{ font: 14px 'Segoe UI', Ubuntu, Sans-Serif; fill: #8b949e; }}
    .value {{ font: bold 14px 'Segoe UI', Ubuntu, Sans-Serif; fill: #e6edf3; }}
    .icon {{ fill: #8b949e; }}
  </style>
  <rect x="0.5" y="0.5" rx="6" ry="6" width="449" height="194" fill="#0d1117" stroke="#30363d" stroke-width="1"/>
  <text x="25" y="35" class="title">GitHub Stats</text>
  
  <g transform="translate(25, 55)">
    <!-- Stars -->
    <g transform="translate(0, 0)">
      <svg class="icon" viewBox="0 0 16 16" width="16" height="16">
        <path fill-rule="evenodd" d="M8 .25a.75.75 0 01.673.418l1.882 3.815 4.21.612a.75.75 0 01.416 1.279l-3.046 2.97.719 4.192a.75.75 0 01-1.088.791L8 12.347l-3.766 1.98a.75.75 0 01-1.088-.79l.72-4.194L.818 6.374a.75.75 0 01.416-1.28l4.21-.611L7.327.668A.75.75 0 018 .25z"/>
      </svg>
      <text x="25" y="12.5" class="label">Total Stars:</text>
      <text x="130" y="12.5" class="value">{stars}</text>
    </g>

    <!-- Commits -->
    <g transform="translate(0, 22)">
      <svg class="icon" viewBox="0 0 16 16" width="16" height="16">
        <path fill-rule="evenodd" d="M10.5 7.75a2.5 2.5 0 11-5 0 2.5 2.5 0 015 0zm1.43.75a4.002 4.002 0 01-7.86 0H.75a.75.75 0 110-1.5h3.32a4.001 4.001 0 017.86 0h3.32a.75.75 0 110 1.5h-3.32z"/>
      </svg>
      <text x="25" y="12.5" class="label">Total Commits:</text>
      <text x="130" y="12.5" class="value">{commits}</text>
    </g>

    <!-- PRs -->
    <g transform="translate(0, 44)">
      <svg class="icon" viewBox="0 0 16 16" width="16" height="16">
        <path fill-rule="evenodd" d="M7.177 3.073L9.573.677A.25.25 0 0110 .854v4.792a.25.25 0 01-.427.177L7.177 3.427a.25.25 0 010-.354zM3.75 2.5a.75.75 0 100 1.5.75.75 0 000-1.5zm-2.25.75a2.25 2.25 0 113 2.122v5.256a2.251 2.251 0 11-1.5 0V5.372A2.25 2.25 0 011.5 3.25zM11 2.5h-1V4h1a1 1 0 011 1v5.628a2.251 2.251 0 101.5 0V5A2.5 2.5 0 0011 2.5zm1 10.25a.75.75 0 111.5 0 .75.75 0 01-1.5 0zM3.75 12a.75.75 0 100 1.5.75.75 0 000-1.5z"/>
      </svg>
      <text x="25" y="12.5" class="label">Pull Requests:</text>
      <text x="130" y="12.5" class="value">{prs}</text>
    </g>

    <!-- Issues -->
    <g transform="translate(0, 66)">
      <svg class="icon" viewBox="0 0 16 16" width="16" height="16">
        <path fill-rule="evenodd" d="M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM0 8a8 8 0 1116 0A8 8 0 010 8zm9 3a1 1 0 11-2 0 1 1 0 012 0zm-.25-6.25a.75.75 0 00-1.5 0v3.5a.75.75 0 001.5 0v-3.5z"/>
      </svg>
      <text x="25" y="12.5" class="label">Issues Opened:</text>
      <text x="130" y="12.5" class="value">{issues}</text>
    </g>

    <!-- Forks -->
    <g transform="translate(0, 88)">
      <svg class="icon" viewBox="0 0 16 16" width="16" height="16">
        <path fill-rule="evenodd" d="M5 3.25a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm0 2.122a2.25 2.25 0 10-1.5 0v.878A2.25 2.25 0 005.75 8.5h1.5v2.128a2.251 2.251 0 101.5 0V8.5h1.5A2.25 2.25 0 0012.5 6.25v-.878a2.25 2.25 0 10-1.5 0v.878a.75.75 0 01-.75.75h-4.5A.75.75 0 015 6.25v-.878zM11 3.25a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm-3 9a.75.75 0 100-1.5.75.75 0 000 1.5z"/>
      </svg>
      <text x="25" y="12.5" class="label">Total Forks:</text>
      <text x="130" y="12.5" class="value">{forks}</text>
    </g>

    <!-- Followers -->
    <g transform="translate(0, 110)">
      <svg class="icon" viewBox="0 0 16 16" width="16" height="16">
        <path fill-rule="evenodd" d="M7 14s-1 0-1-1 1-4 5-4 5 3 5 4-1 1-1 1H7zm4-6a3 3 0 100-6 3 3 0 000 6zm-5.784 6A2.24 2.24 0 015 13c0-1.355.68-2.75 1.936-3.72A6.325 6.325 0 005 9c-4 0-5 3-5 4 0 1 1 1 1 1h4.216zM4.5 8a2.5 2.5 0 100-5 2.5 2.5 0 000 5z"/>
      </svg>
      <text x="25" y="12.5" class="label">Followers:</text>
      <text x="130" y="12.5" class="value">{followers}</text>
    </g>
  </g>

  <!-- GitHub Logo Graphic -->
  <g transform="translate(290, 42)">
    <svg width="110" height="110" viewBox="0 0 16 16" style="fill: #30363d; opacity: 0.15;">
      <path fill-rule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/>
    </svg>
  </g>
</svg>"""
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        out.write_text(svg_content, encoding="utf-8")
        log.info("✅ github-stats.svg written with XML template.")
    except Exception as exc:
        log.error("make_stats_svg failed: %s", exc)
        # Only fall back if file doesn't exist or is empty
        if not out.exists() or out.stat().st_size < 100:
            _fallback_copy("github-stats.svg", out)
        else:
            log.info("Keeping existing github-stats.svg (error during re-render, but old file is valid)")


def make_languages_svg(data: dict) -> None:
    out = ASSETS_DIR / "languages.svg"
    try:
        repos = data["user"]["repositories"]["nodes"]
        lang_data: dict[str, dict] = {}
        total_repos_with_lang = 0
        for r in repos:
            if r.get("primaryLanguage"):
                n = r["primaryLanguage"]["name"]
                c = r["primaryLanguage"]["color"] or "#8b949e"
                if n not in lang_data:
                    lang_data[n] = {"count": 0, "color": c}
                lang_data[n]["count"] += 1
                total_repos_with_lang += 1

        # NEVER invent data — if no real languages detected, skip rendering
        if not lang_data:
            log.warning("No language data detected in repositories — skipping languages.svg")
            # Keep existing file if it exists; don't replace with fake data
            if not out.exists():
                _write_minimal_svg(out, "No language data available")
            return

        sorted_langs = sorted(lang_data.items(), key=lambda x: x[1]["count"], reverse=True)[:6]

        # Convert hex color to grayscale
        def hex_to_gray(hex_color):
            try:
                hex_color = hex_color.lstrip('#')
                if len(hex_color) == 6:
                    r, g, b = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
                    gray = int(0.299 * r + 0.587 * g + 0.114 * b)
                    gray = max(gray, 100)
                    return f"#{gray:02x}{gray:02x}{gray:02x}"
            except:
                pass
            return "#8b949e"

        # Calculate percentages
        languages_list = []
        for name, info in sorted_langs:
            pct = (info["count"] / total_repos_with_lang) * 100 if total_repos_with_lang > 0 else 0
            languages_list.append({
                "name": name,
                "color": hex_to_gray(info["color"]),
                "percentage": pct
            })

        # Calculate remainder for Others if needed
        top_pct_sum = sum(l["percentage"] for l in languages_list)
        if top_pct_sum < 100.0 and len(lang_data) > 6:
            languages_list.append({
                "name": "Others",
                "color": "#8b949e",
                "percentage": 100.0 - top_pct_sum
            })

        svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" width="450" height="195" viewBox="0 0 450 195">
  <style>
    .title {{ font: bold 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: #e6edf3; }}
    .lang-name {{ font: bold 12px 'Segoe UI', Ubuntu, Sans-Serif; fill: #e6edf3; }}
    .lang-pct {{ font: 12px 'Segoe UI', Ubuntu, Sans-Serif; fill: #8b949e; }}
  </style>
  <rect x="0.5" y="0.5" rx="6" ry="6" width="449" height="194" fill="#0d1117" stroke="#30363d" stroke-width="1"/>
  <text x="25" y="35" class="title">Top Languages</text>
  
  <!-- Progress Bar -->
  <clipPath id="bar-clip">
    <rect x="25" y="55" width="400" height="10" rx="5" />
  </clipPath>
  <g clip-path="url(#bar-clip)">
"""
        current_x = 25
        for lang in languages_list:
            width = 400 * (lang["percentage"] / 100.0)
            if width > 0:
                svg_content += f'    <rect x="{current_x:.2f}" y="55" width="{width:.2f}" height="10" fill="{lang["color"]}" />\n'
                current_x += width

        svg_content += """  </g>
  
  <!-- Legend Grid -->
  <g transform="translate(25, 85)">
"""
        for i, lang in enumerate(languages_list[:6]):
            col = i // 3
            row = i % 3
            x = col * 200
            y = row * 24
            svg_content += f"""    <g transform="translate({x}, {y})">
      <circle cx="5" cy="8" r="5" fill="{lang["color"]}" />
      <text x="18" y="12" class="lang-name">{lang["name"]}</text>
      <text x="110" y="12" class="lang-pct">{lang["percentage"]:.1f}%</text>
    </g>
"""

        svg_content += """  </g>
</svg>"""
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        out.write_text(svg_content, encoding="utf-8")
        log.info("✅ languages.svg written with XML template.")
    except Exception as exc:
        log.error("make_languages_svg failed: %s", exc)
        # Only fall back if file doesn't exist or is empty
        if not out.exists() or out.stat().st_size < 100:
            _fallback_copy("languages.svg", out)
        else:
            log.info("Keeping existing languages.svg (error during re-render, but old file is valid)")


def make_streak_svg(data: dict) -> None:
    out = ASSETS_DIR / "streak.svg"
    try:
        weeks = data["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
        current, longest = calculate_streak(weeks)
        total = data["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"]

        log.info("Rendering streak with real data: current=%d, longest=%d, total=%d", current, longest, total)

        svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" width="450" height="195" viewBox="0 0 450 195">
  <style>
    .title {{ font: bold 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: #e6edf3; }}
    .label {{ font: 14px 'Segoe UI', Ubuntu, Sans-Serif; fill: #8b949e; }}
    .value {{ font: bold 36px 'Segoe UI', Ubuntu, Sans-Serif; fill: #e6edf3; }}
    .unit {{ font: 12px 'Segoe UI', Ubuntu, Sans-Serif; fill: #8b949e; }}
    .stat-label {{ font: 13px 'Segoe UI', Ubuntu, Sans-Serif; fill: #8b949e; }}
    .stat-val {{ font: bold 13px 'Segoe UI', Ubuntu, Sans-Serif; fill: #e6edf3; }}
    .icon {{ fill: #8b949e; }}
  </style>
  <rect x="0.5" y="0.5" rx="6" ry="6" width="449" height="194" fill="#0d1117" stroke="#30363d" stroke-width="1"/>
  
  <!-- Title with Fire Icon -->
  <g transform="translate(25, 35)">
    <svg class="icon" viewBox="0 0 16 16" width="20" height="20" style="vertical-align: middle;">
      <path fill-rule="evenodd" d="M8.618.067a.75.75 0 00-.736.035C6.096 1.34 4 3.758 4 6.5c0 2.373 1.272 4.316 2.977 5.253C6.398 12.016 6 12.656 6 13.5c0 1.38 1.12 2.5 2.5 2.5s2.5-1.12 2.5-2.5c0-.844-.398-1.484-.977-1.747 1.705-.937 2.977-2.88 2.977-5.253 0-2.742-2.096-5.16-3.882-6.398a.75.75 0 00-.502-.135zM8.5 14.5c-.552 0-1-.448-1-1s.448-1 1-1 1 .448 1 1-.448 1-1 1"/>
    </svg>
    <text x="28" y="16" class="title">Current Streak</text>
  </g>
  
  <!-- Big Streak Counter -->
  <g transform="translate(225, 95)" text-anchor="middle">
    <text class="value">{current}</text>
    <text y="22" class="unit">days</text>
  </g>
  
  <!-- Stats Footer -->
  <g transform="translate(25, 155)">
    <text class="stat-label">Longest Streak:</text>
    <text x="110" class="stat-val">{longest} days</text>
    
    <text x="220" class="stat-label">Total Contributions:</text>
    <text x="350" class="stat-val">{total}</text>
  </g>
</svg>"""
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        out.write_text(svg_content, encoding="utf-8")
        # Also write to assets/streak/ for legacy path used in README
        (Path("assets/streak") / "streak.svg").write_text(svg_content, encoding="utf-8")
        log.info("✅ streak.svg written with XML template.")
    except Exception as exc:
        log.error("make_streak_svg failed: %s", exc)
        # Only fall back if file doesn't exist or is empty
        if not out.exists() or out.stat().st_size < 100:
            _fallback_copy("streak.svg", out)
        else:
            log.info("Keeping existing streak.svg (error during re-render, but old file is valid)")


def make_graph_svg(data: dict) -> None:
    """Download Vercel activity graph; fall back to local contribution heatmap on failure."""
    out = ASSETS_DIR / "contribution-graph.svg"
    url = (
        f"https://github-readme-activity-graph.vercel.app/graph"
        f"?username={USERNAME}&theme=github-dark&hide_border=true"
        f"&bg_color=0d1117&color=58a6ff&line=58a6ff&point=c9d1d9"
        f"&area=true&area_color=1f6feb&height=300&cache_seconds=900"
    )
    try:
        r = _get(url, retries=3)
        if "<svg" in r.text.lower():
            out.write_text(r.text, encoding="utf-8")
            log.info("✅ contribution-graph.svg downloaded from Vercel (real data).")
            return
        raise ValueError("Response body did not contain <svg")
    except Exception as exc:
        log.warning("Vercel graph fetch failed: %s — generating local heatmap from real GitHub data.", exc)

    # Local heatmap fallback (using real contribution calendar)
    try:
        weeks = data["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
        all_days = [d for w in weeks for d in w["contributionDays"]]
        counts = [d["contributionCount"] for d in all_days]
        cols = len(weeks)
        rows = 7

        log.info("Generating local heatmap with %d weeks of real contribution data", cols)

        grid = [[0] * cols for _ in range(rows)]
        for wi, week in enumerate(weeks):
            for day in week["contributionDays"]:
                dow = datetime.fromisoformat(day["date"]).weekday()
                grid[dow][wi] = day["contributionCount"]

        fig, ax = plt.subplots(figsize=(12, 2.4), dpi=100)
        fig.patch.set_facecolor("#0d1117"); ax.set_facecolor("#0d1117"); ax.axis("off")

        cell = 0.85
        palette = ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"]
        mx = max(max(row) for row in grid) or 1
        for c, col in enumerate(zip(*grid)):
            for r, val in enumerate(col):
                level = min(int(val / mx * 4 + 0.5), 4) if val else 0
                rect = plt.Rectangle((c * (cell + 0.15), (6 - r) * (cell + 0.15)), cell, cell,
                                     color=palette[level], linewidth=0)
                ax.add_patch(rect)

        ax.set_xlim(-0.5, cols * (cell + 0.15) + 0.5)
        ax.set_ylim(-0.5, 8 * (cell + 0.15) + 0.5)
        plt.tight_layout(pad=0)
        _save_svg(fig, out)
        log.info("✅ Local contribution heatmap generated with real data (Vercel fallback).")
    except Exception as exc2:
        log.error("Local heatmap fallback also failed: %s", exc2)
        # Only fall back if file doesn't exist or is empty
        if not out.exists() or out.stat().st_size < 100:
            _fallback_copy("contribution-graph.svg", out)
        else:
            log.info("Keeping existing contribution-graph.svg (error during re-render, but old file is valid)")


def download_summary_cards() -> None:
    """Download GitHub Profile Summary Cards from Vercel; keep existing file on failure (never invent)."""
    cards = {
        "summary-repos-per-language.svg":  f"https://github-profile-summary-cards.vercel.app/api/cards/repos-per-language?username={USERNAME}&theme=github_dark",
        "summary-most-commit-language.svg":f"https://github-profile-summary-cards.vercel.app/api/cards/most-commit-language?username={USERNAME}&theme=github_dark",
        "summary-productive-time.svg":     f"https://github-profile-summary-cards.vercel.app/api/cards/productive-time?username={USERNAME}&theme=github_dark&utcOffset=5.5",
    }
    for fname, url in cards.items():
        dest = ASSETS_DIR / fname
        try:
            r = _get(url, retries=3)
            if "<svg" not in r.text.lower():
                raise ValueError("Not an SVG response")
            
            svg = r.text

            def grayscale_match(match):
                hex_col = match.group(0).lower()
                if hex_col in ["#0d1117", "#161b22", "#30363d", "#27272a", "#8b949e", "#e6edf3", "#e5e7eb", "#ffffff", "#000000", "#121212"]:
                    return hex_col
                h = hex_col.lstrip('#')
                if len(h) == 6:
                    r_val, g_val, b_val = tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
                    gray = max(int(0.299 * r_val + 0.587 * g_val + 0.114 * b_val), 100)
                    return f"#{gray:02x}{gray:02x}{gray:02x}"
                return hex_col

            svg = re.sub(r'#[0-9a-fA-F]{6}', grayscale_match, svg)

            svg = re.sub(
                r'(<text\s+x="30"\s+y="40"\s+style="font-size:\s*22px;\s*fill:\s*)[^;"]+',
                r'\g<1>#ffffff',
                svg,
            )
            dest.write_text(svg, encoding="utf-8")
            log.info("✅ %s downloaded (real Vercel data).", fname)
        except Exception as exc:
            if dest.exists():
                log.warning("Download failed for %s: %s — keeping existing file (will retry next cycle).", fname, exc)
            else:
                # Only write placeholder if file never existed; never replace good data with fake
                log.warning("Download failed for %s: %s — no existing file, writing placeholder.", fname, exc)
                _write_minimal_svg_with_marker(dest, f"Waiting for {fname.split('.')[0]}", placeholder=True)


def update_achievements() -> None:
    """Scrape GitHub achievements and update README between marker comments."""
    log.info("🏆 Scraping GitHub achievements…")
    url = f"https://github.com/{USERNAME}"
    hdrs = {"User-Agent": "Mozilla/5.0"}

    try:
        r = _get(url, headers=hdrs, retries=2)
        soup = BeautifulSoup(r.text, "html.parser")
        badges: list[tuple[str, str]] = []

        # Strategy 1: semantic header search
        for tag in ("h2", "h3", "h4", "span", "div"):
            hdr = soup.find(tag, string=lambda t: t and "achievements" in t.lower())
            if hdr:
                container = hdr.parent
                for _ in range(4):
                    if container is None:
                        break
                    for img in container.find_all("img"):
                        src = img.get("src", "")
                        alt = img.get("alt", "")
                        if src and alt and ("badge" in src or "achievement" in src or "githubassets" in src):
                            if alt not in {b[0] for b in badges}:
                                badges.append((alt, src))
                    if badges:
                        break
                    container = container.parent

        # Strategy 2: CSS selector
        if not badges:
            for el in soup.select('a[href*="/achievements/"]'):
                img = el.find("img")
                if img and img.get("src") and img.get("alt"):
                    badges.append((img["alt"], img["src"]))

        # Strategy 3: known alt-text scan
        if not badges:
            known = {"pull shark", "yolo", "quickdraw", "galaxy brain", "starstruck",
                     "pair extraordinaire", "public sponsor"}
            for img in soup.find_all("img"):
                alt = img.get("alt", "")
                src = img.get("src", "")
                if alt and src and any(k in alt.lower() for k in known):
                    badges.append((alt, src))

        if not badges:
            log.info("No achievements scraped — keeping existing README section.")
            return

        BADGES_DIR.mkdir(parents=True, exist_ok=True)
        md_imgs: list[str] = []

        for alt, src in badges:
            safe = re.sub(r"[^a-zA-Z0-9_-]", "", alt.lower().replace(" ", "_"))
            ext = Path(src.split("?")[0]).suffix or ".png"
            fname = f"{safe}{ext}"
            fpath = BADGES_DIR / fname
            try:
                ir = _get(src, retries=2)
                fpath.write_bytes(ir.content)
                md_imgs.append(f'<img src="assets/badges/{fname}" width="75px" alt="{alt}" title="{alt}" />')
            except Exception as exc:
                log.warning("Badge download failed (%s): %s — using remote URL.", alt, exc)
                md_imgs.append(f'<img src="{src}" width="75px" alt="{alt}" title="{alt}" />')

        readme = Path("README.md")
        if not readme.exists():
            log.warning("README.md not found — skipping achievement inject.")
            return

        content = readme.read_text("utf-8")
        start, end = "<!-- START_SECTION:achievements -->", "<!-- END_SECTION:achievements -->"
        if start in content and end in content:
            new_block = start + "\n" + " ".join(md_imgs) + "\n" + end
            content = re.sub(rf"{re.escape(start)}.*?{re.escape(end)}", new_block, content, flags=re.DOTALL)
            readme.write_text(content, encoding="utf-8")
            log.info("✅ Achievements injected into README.md.")
        else:
            log.warning("Achievement markers not found in README.md — skipping inject.")

    except Exception as exc:
        log.error("update_achievements failed: %s", exc)


def update_recent_repos(data: dict) -> None:
    """Inject most recently pushed repositories into README."""
    log.info("📝 Updating recent repos in README…")
    try:
        r = requests.get(f"https://api.github.com/users/{USERNAME}/repos?sort=pushed&per_page=5", timeout=10)
        r.raise_for_status()
        repos = r.json()
        recent = [r for r in repos if r["name"] != USERNAME][:3]
        
        md_lines = ["| `repo` | `description` | `last active` |", "|--------|---------------|---------------|"]
        for r in recent:
            name = r["name"]
            url = r["html_url"]
            desc = (r.get("description") or "No description").strip()
            # truncate long descriptions
            if len(desc) > 50:
                desc = desc[:47] + "..."
            pushed_dt = datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00"))
            pushed_str = pushed_dt.strftime("%b %d, %Y")
            
            # format name for shields.io (dashes to double dashes, underscores to double underscores)
            shield_name = name.replace("-", "--").replace("_", "__").replace(" ", "_")
            badge_url = f"https://img.shields.io/badge/{shield_name}-1a1a1a?style=flat-square&logo=github&logoColor=white"
            repo_col = f"[![{name}]({badge_url})]({url})"
            md_lines.append(f"| {repo_col} | {desc} | {pushed_str} |")
        
        readme = Path("README.md")
        if not readme.exists():
            return
            
        content = readme.read_text("utf-8")
        start, end = "<!-- START_SECTION:recent_repos -->", "<!-- END_SECTION:recent_repos -->"
        if start in content and end in content:
            new_block = start + "\n" + "\n".join(md_lines) + "\n" + end
            content = re.sub(rf"{re.escape(start)}.*?{re.escape(end)}", new_block, content, flags=re.DOTALL)
            readme.write_text(content, encoding="utf-8")
            log.info("✅ Recent repos injected into README.md.")
    except Exception as exc:
        log.error("update_recent_repos failed: %s", exc)


# ─── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    log.info("🚀 Starting robust stats generator — %s", datetime.now(timezone.utc).isoformat())
    log.info("Policy: NEVER show false data. Real data (fresh or cached) > old data > skip rendering. Never invent numbers.")
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    Path("assets/streak").mkdir(parents=True, exist_ok=True)

    # 1. Fetch data (live → cache → error)
    try:
        data = get_github_data()
        log.info("✅ Data source confirmed. Rendering all cards with REAL statistics.")
    except RuntimeError as exc:
        log.critical("Cannot fetch any data: %s", exc)
        sys.exit(1)

    # 2. Render all cards (each handles its own error → fallback)
    # Each card now prefers keeping old real data over writing a placeholder
    make_stats_svg(data)
    make_languages_svg(data)
    make_streak_svg(data)
    make_graph_svg(data)

    # 3. Download external summary cards
    download_summary_cards()

    # 4. Update achievements in README
    update_achievements()

    # 5. Update recent repos in README
    update_recent_repos(data)

    log.info("✨ Stats generation complete. All SVGs contain REAL data or are preserved from the last successful run.")


if __name__ == "__main__":
    main()
