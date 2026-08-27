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
    repositories(first: 100, ownerAffiliations: OWNER, privacy: PUBLIC) {
      totalCount
      nodes {
        name
        stargazerCount
        forkCount
        primaryLanguage { name color }
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

        # ✅ Log what we're rendering with real numbers for audit trail
        log.info("Rendering stats with real data: %d stars, %d forks, %d commits", stars, forks, cc["totalCommitContributions"])

        fig = plt.figure(figsize=(6, 4), dpi=100)
        ax  = fig.add_axes([0, 0, 1, 1])
        ax.axis("off")
        fig.patch.set_facecolor(BG)

        ax.add_patch(_rounded_box(ax))

        items = [
            ("⭐", "Total Stars",    stars),
            ("💻", "Total Commits",  cc["totalCommitContributions"] + cc.get("restrictedContributionsCount", 0)),
            ("🔀", "Pull Requests",  cc["totalPullRequestContributions"]),
            ("🐛", "Issues Opened",  cc["totalIssueContributions"]),
            ("🍴", "Total Forks",    forks),
            ("👥", "Followers",      user["followers"]["totalCount"]),
        ]

        y = 0.83
        for emoji, label, val in items:
            ax.text(0.08, y, f"{emoji}  {label}:", fontsize=11.5, color=MUTED,   fontfamily="sans-serif")
            ax.text(0.72, y, str(val),             fontsize=11.5, color=ACCENT,  fontfamily="sans-serif", fontweight="bold", ha="right")
            y -= 0.135

        logo = _get_github_logo()
        if logo:
            img = plt.imread(str(logo))
            if img.ndim == 3 and img.shape[2] == 4:
                img = img.copy(); img[:, :, :3] = 1.0   # white octocat
            la = fig.add_axes([0.68, 0.30, 0.24, 0.36])
            la.imshow(img); la.axis("off")

        _save_svg(fig, out)
        log.info("✅ github-stats.svg written with real data.")
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
        counts: dict[str, int] = {}
        for r in repos:
            if r.get("primaryLanguage"):
                n = r["primaryLanguage"]["name"]
                counts[n] = counts.get(n, 0) + 1

        # ❌ NEVER invent data — if no real languages detected, skip rendering
        if not counts:
            log.warning("No language data detected in repositories — skipping languages.svg")
            # Keep existing file if it exists; don't replace with fake data
            if not out.exists():
                _write_minimal_svg(out, "No language data available")
            return

        labels = list(counts)[:6]
        values = [counts[l] for l in labels]

        fig, ax = plt.subplots(figsize=(6, 4), dpi=100)
        fig.patch.set_facecolor(BG); ax.set_facecolor(CARD)

        box = _rounded_box(ax, fig)
        box.set_transform(fig.transFigure)
        fig.patches.append(box)

        wedges, texts, autotexts = ax.pie(
            values, labels=labels, autopct="%1.0f%%", startangle=90,
            colors=COLORS[: len(labels)], pctdistance=0.75,
            textprops={"color": "white", "fontsize": 11, "fontweight": "bold", "fontfamily": "sans-serif"},
        )
        for at in autotexts:
            at.set_color("#111827"); at.set_fontsize(9)

        ax.add_artist(plt.Circle((0, 0), 0.50, fc=CARD, ec=GRID, linewidth=1))
        ax.axis("equal")
        plt.tight_layout()
        _save_svg(fig, out)
        log.info("✅ languages.svg written with real data: %s", counts)
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

        fig, ax = plt.subplots(figsize=(7, 4), dpi=100)
        fig.patch.set_facecolor(BG); ax.set_facecolor(CARD); ax.axis("off")

        ax.add_patch(FancyBboxPatch((0.05, 0.05), 0.9, 0.9, boxstyle="round,pad=0.02",
                                    linewidth=1.2, edgecolor=GRID, facecolor=CARD))

        ax.text(0.5, 0.84, "🔥 Current Streak", color=TEXT, fontsize=17, ha="center")
        ax.text(0.5, 0.55, str(current),         color=ACCENT, fontsize=36, ha="center", fontweight="bold")
        ax.text(0.5, 0.40, "days",               color=MUTED, fontsize=12, ha="center")
        ax.text(0.5, 0.24, f"Longest Streak : {longest}", color=TEXT, fontsize=11, ha="center")
        ax.text(0.5, 0.10, f"Total Contributions : {total}", color=MUTED, fontsize=9, ha="center")

        _save_svg(fig, out)
        # Also write to assets/streak/ for legacy path used in README
        (Path("assets/streak") / "streak.svg").write_text(out.read_text("utf-8"), encoding="utf-8")
        log.info("✅ streak.svg written with real data.")
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


# ─── Monochrome post-processor ────────────────────────────────────────────────
def _apply_monochrome(svg_text: str) -> str:
    """Inject an SVG grayscale filter into a downloaded SVG, wrapping all content."""
    try:
        from lxml import etree
        NS = "http://www.w3.org/2000/svg"
        root = etree.fromstring(svg_text.encode("utf-8"))

        # Ensure <defs> exists at position 0
        defs = root.find(f"{{{NS}}}defs")
        if defs is None:
            defs = etree.Element(f"{{{NS}}}defs")
            root.insert(0, defs)

        # Add luminance-weighted grayscale feColorMatrix filter
        filt = etree.SubElement(defs, f"{{{NS}}}filter")
        filt.set("id", "mono")
        filt.set("x", "0")
        filt.set("y", "0")
        filt.set("width", "100%")
        filt.set("height", "100%")
        matrix = etree.SubElement(filt, f"{{{NS}}}feColorMatrix")
        matrix.set("type", "matrix")
        matrix.set("values",
            "0.299 0.587 0.114 0 0 "
            "0.299 0.587 0.114 0 0 "
            "0.299 0.587 0.114 0 0 "
            "0     0     0     1 0")

        # Wrap all non-defs children in a <g filter="url(#mono)">
        children = [c for c in root if c.tag != f"{{{NS}}}defs"]
        if children:
            g = etree.Element(f"{{{NS}}}g")
            g.set("filter", "url(#mono)")
            first_idx = list(root).index(children[0])
            root.insert(first_idx, g)
            for child in children:
                root.remove(child)
                g.append(child)

        return etree.tostring(root, encoding="unicode", xml_declaration=False)
    except Exception as exc:
        log.warning("Monochrome conversion failed: %s — returning original SVG.", exc)
        return svg_text


def download_summary_cards() -> None:
    """Download GitHub Profile Summary Cards from Vercel; keep existing file on failure (never invent)."""
    cards = {
        "summary-stats.svg":               f"https://github-profile-summary-cards.vercel.app/api/cards/stats?username={USERNAME}&theme=github_dark",
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
            svg = re.sub(
                r'(<text\s+x="30"\s+y="40"\s+style="font-size:\s*22px;\s*fill:\s*)[^;"]+',
                r'\g<1>#ffffff',
                r.text,
            )
            dest.write_text(_apply_monochrome(svg), encoding="utf-8")
            log.info("✅ %s downloaded and converted to monochrome.", fname)
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

    log.info("✨ Stats generation complete. All SVGs contain REAL data or are preserved from the last successful run.")


if __name__ == "__main__":
    main()
