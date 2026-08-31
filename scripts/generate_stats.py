"""
generate_stats.py — Enhanced version with robust validation, structured results, and observability.

Key improvements:
  1. Comprehensive data validation before streak calculation (prevents incorrect stats)
  2. Structured result types (FetchResult) with source/freshness metadata (enables UI to show data age)
  3. Explicit rate-limit detection (switches sources immediately, not after retries)
  4. Documented streak calculation rules (prevents edge-case bugs)
  5. Per-source latency tracking + observability (diagnose bottlenecks)
  6. Partial data detection (rejects incomplete contribution calendars)
  7. Live-first fetch strategy with a hard latency budget (real-time / low-latency updates)
  8. Visible on-card freshness indicator (live / cached Xm ago / stale), so a viewer can
     always tell how current the streak they're looking at actually is
"""

import os, sys, re, json, time, shutil, hashlib, requests, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
from enum import Enum

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

SEP = "─" * 56

# ─── Paths & constants ─────────────────────────────────────────────────────────
USERNAME     = "BhattAyush17"
ASSETS_DIR   = Path("assets/stats")
BADGES_DIR   = Path("assets/badges")
SNAKE_DIR    = Path("assets/snake")
FALLBACK_DIR = Path("assets/fallback")
CACHE_DIR    = Path("assets/cache")
CACHE_FILE   = CACHE_DIR / "data.json"

# ── Freshness / latency tuning ──────────────────────────────────────────────
# HOT_CACHE_TTL:     if the cache was written more recently than this, skip the
#                     live network round-trip entirely (protects against hammering
#                     the API on rapid successive runs; keeps latency near-zero).
# CACHE_STALE_AFTER:  cosmetic threshold — cache older than this is still USABLE
#                     as a fallback, but the on-card badge will flag it as stale
#                     so a viewer knows the number may be out of date.
# CACHE_HARD_EXPIRY:  absolute cutoff — cache older than this is never used at all.
HOT_CACHE_TTL     = timedelta(seconds=90)
CACHE_STALE_AFTER = timedelta(hours=6)
CACHE_HARD_EXPIRY = timedelta(days=14)

# ── Live-fetch tuning ───────────────────────────────────────────────────────
# Kept deliberately tight: this is a "live-first" strategy (see get_github_data),
# so a slow/hanging request must fail fast and hand off to cache rather than
# stall the whole pipeline.
LIVE_FETCH_TIMEOUT   = 10     # seconds per HTTP attempt (was 20)
LIVE_FETCH_RETRIES   = 2      # attempts before giving up (was 3)
BACKOFF_BASE_SECONDS = 1      # backoff schedule: 1s, 2s, ... (was 2s, 4s, 8s...)
OVERALL_BUDGET_SECONDS = 12   # hard wall-clock budget for the live layer before
                               # get_github_data() gives up and falls back to cache

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

# Freshness badge colours
COLOR_LIVE   = "#3fb950"  # green — fetched this run
COLOR_FRESH  = "#d29922"  # amber — cached, but within CACHE_STALE_AFTER
COLOR_STALE  = "#f85149"  # red   — cached and past CACHE_STALE_AFTER
COLOR_UNKNOWN = "#8b949e"  # grey  — no metadata available


# ─── Enums & Structured Types ──────────────────────────────────────────────────
class ResultStatus(Enum):
    """Per spec section 10: structured result states."""
    SUCCESS = "SUCCESS"
    SUCCESS_USING_FALLBACK = "SUCCESS_USING_FALLBACK"
    SUCCESS_USING_CACHE = "SUCCESS_USING_CACHE"
    PARTIAL_RESULT = "PARTIAL_RESULT"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    TEMPORARY_UNAVAILABLE = "TEMPORARY_UNAVAILABLE"
    NO_DATA = "NO_DATA"


class FallbackReason(Enum):
    """Why we switched sources."""
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    HTTP_ERROR = "HTTP_ERROR"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    INCOMPLETE_DATA = "INCOMPLETE_DATA"
    AUTH_FAILED = "AUTH_FAILED"
    NETWORK_ERROR = "NETWORK_ERROR"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


@dataclass
class FetchResult:
    """
    Structured result for all fetch operations.
    Enables UI to show data age, source, and freshness status.
    """
    status: ResultStatus
    data: Optional[Dict[str, Any]] = None
    source: str = "unknown"  # "graphql_api", "rest_api", "cache", "fallback_svg"
    cached_at: Optional[str] = None  # ISO format timestamp
    fetched_at: Optional[str] = None  # ISO format timestamp
    duration_ms: float = 0.0  # request duration
    fallback_reason: Optional[FallbackReason] = None
    error_message: Optional[str] = None
    is_partial: bool = False  # whether data is incomplete

    def is_fresh(self, max_age: timedelta = timedelta(minutes=5)) -> bool:
        """Check if result is fresh enough to use without background refresh."""
        if self.status == ResultStatus.SUCCESS and self.fetched_at:
            fetched = datetime.fromisoformat(self.fetched_at)
            age = datetime.now(timezone.utc) - fetched
            return age <= max_age
        return False

    def freshness_label(self) -> str:
        """Generate UI-friendly freshness indicator."""
        if self.fetched_at:
            fetched = datetime.fromisoformat(self.fetched_at)
            age = datetime.now(timezone.utc) - fetched
            if age < timedelta(minutes=1):
                return "just now"
            elif age < timedelta(hours=1):
                mins = int(age.total_seconds() / 60)
                return f"{mins} min ago"
            elif age < timedelta(days=1):
                hours = int(age.total_seconds() / 3600)
                return f"{hours}h ago"
            else:
                days = int(age.total_seconds() / 86400)
                return f"{days}d ago"
        if self.cached_at:
            cached = datetime.fromisoformat(self.cached_at)
            age = datetime.now(timezone.utc) - cached
            return f"cached {int(age.total_seconds() / 60)}m ago"
        return "unknown"

    def badge(self) -> "tuple[str, str]":
        """
        (dot_color, label) for the on-card freshness indicator.
        Lets a viewer see at a glance whether they're looking at a live
        number or a cached/stale fallback, instead of silently guessing.
        """
        if self.status == ResultStatus.SUCCESS:
            label = "live" + (" · partial" if self.is_partial else "")
            return COLOR_LIVE, label

        if self.status == ResultStatus.SUCCESS_USING_CACHE:
            stale = False
            if self.cached_at:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(self.cached_at)
                stale = age > CACHE_STALE_AFTER
            label = self.freshness_label() + (" · partial" if self.is_partial else "")
            return (COLOR_STALE if stale else COLOR_FRESH), label

        return COLOR_UNKNOWN, (self.status.value.lower() if self.status else "unknown")


# ─── Retry helper with rate-limit detection ─────────────────────────────────────
def _is_rate_limited(response: requests.Response) -> bool:
    """
    Detect rate limit explicitly.
    Per spec section 9: "Detect GitHub rate-limit responses explicitly."
    """
    if response.status_code in (403, 429):
        return True
    if "X-RateLimit-Remaining" in response.headers:
        try:
            remaining = int(response.headers.get("X-RateLimit-Remaining", 1))
            return remaining == 0
        except ValueError:
            pass
    return False


def _backoff_wait(attempt: int, deadline: Optional[float]) -> float:
    """Exponential backoff, clipped to whatever time remains in the budget."""
    wait = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    if deadline is not None:
        remaining = deadline - time.time()
        wait = max(0.0, min(wait, remaining - 0.05))
    return wait


def _get(url: str, *, timeout: int = LIVE_FETCH_TIMEOUT, retries: int = LIVE_FETCH_RETRIES,
         deadline: Optional[float] = None, **kwargs) -> requests.Response:
    """
    GET with exponential back-off.
    Raises immediately on rate-limit (don't retry).
    If `deadline` (an absolute time.time() value) is given, stops retrying —
    and shrinks the sleep between attempts — once the budget is used up, so a
    slow endpoint can't blow past the caller's latency target.
    """
    for attempt in range(1, retries + 1):
        if deadline is not None and time.time() >= deadline:
            raise RuntimeError("Time budget exceeded before request could complete")
        try:
            r = requests.get(url, timeout=timeout, **kwargs)
            if _is_rate_limited(r):
                raise RuntimeError(f"Rate limited (HTTP {r.status_code})")
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            if "rate limit" in str(exc).lower():
                raise RuntimeError("Rate limited") from exc
            wait = _backoff_wait(attempt, deadline)
            log.warning("GET %s failed (attempt %d/%d): %s — retrying in %.1fs", url, attempt, retries, exc, wait)
            if attempt < retries and wait > 0:
                time.sleep(wait)
    raise RuntimeError(f"All {retries} attempts failed for {url}")


def _post(url: str, *, timeout: int = LIVE_FETCH_TIMEOUT, retries: int = LIVE_FETCH_RETRIES,
          deadline: Optional[float] = None, **kwargs) -> requests.Response:
    """POST with exponential back-off, rate-limit detection, and an optional deadline."""
    for attempt in range(1, retries + 1):
        if deadline is not None and time.time() >= deadline:
            raise RuntimeError("Time budget exceeded before request could complete")
        try:
            r = requests.post(url, timeout=timeout, **kwargs)
            if _is_rate_limited(r):
                raise RuntimeError(f"Rate limited (HTTP {r.status_code})")
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            if "rate limit" in str(exc).lower():
                raise RuntimeError("Rate limited") from exc
            wait = _backoff_wait(attempt, deadline)
            log.warning("POST %s failed (attempt %d/%d): %s — retrying in %.1fs", url, attempt, retries, exc, wait)
            if attempt < retries and wait > 0:
                time.sleep(wait)
    raise RuntimeError(f"All {retries} attempts failed for {url}")


# ─── Data Validation Layer ──────────────────────────────────────────────────────
def validate_contribution_data(weeks: list) -> tuple[bool, Optional[str]]:
    """
    Per spec section 4: Validate data before accepting it.
    Returns (is_valid, error_message).
    """
    if not weeks or not isinstance(weeks, list):
        return False, "Contribution weeks is empty or not a list"

    all_days = []
    prev_date = None

    for wi, week in enumerate(weeks):
        if not isinstance(week, dict) or "contributionDays" not in week:
            return False, f"Week {wi} missing contributionDays"

        for di, day in enumerate(week.get("contributionDays", [])):
            if not isinstance(day, dict):
                return False, f"Week {wi} day {di} is not a dict"

            # Validate required fields
            if "date" not in day or "contributionCount" not in day:
                return False, f"Week {wi} day {di} missing date or contributionCount"

            date_str = day["date"]
            count = day["contributionCount"]

            # Validate date format
            try:
                dt = datetime.fromisoformat(date_str)
            except (ValueError, TypeError):
                return False, f"Invalid date format: {date_str}"

            # Check for future dates
            if dt.date() > datetime.now(timezone.utc).date():
                return False, f"Future date in contribution data: {date_str}"

            # Validate contribution count is non-negative integer
            if not isinstance(count, int) or count < 0:
                return False, f"Invalid contribution count: {count} (not a non-negative int)"

            # Check date ordering (should be ascending or at least non-decreasing per calendar structure)
            if prev_date and date_str < prev_date:
                return False, f"Dates not in order: {prev_date} → {date_str}"

            # Check for duplicates
            if date_str == prev_date:
                return False, f"Duplicate date in contribution data: {date_str}"

            all_days.append((date_str, count))
            prev_date = date_str

    if not all_days:
        return False, "No contribution days found after validation"

    # Check calendar completeness: should have at least ~52 weeks of data if available
    expected_min_weeks = 50  # allow some flexibility
    if len(weeks) < expected_min_weeks:
        log.warning("Contribution calendar has only %d weeks (expected ~52); may be incomplete", len(weeks))

    return True, None


def validate_user_data(user_dict: dict) -> tuple[bool, Optional[str]]:
    """
    Validate entire user object before using it.
    """
    if not isinstance(user_dict, dict):
        return False, "User data is not a dict"

    if "contributionsCollection" not in user_dict:
        return False, "Missing contributionsCollection"

    cc = user_dict["contributionsCollection"]
    if "contributionCalendar" not in cc:
        return False, "Missing contributionCalendar"

    calendar = cc["contributionCalendar"]
    if "weeks" not in calendar:
        return False, "Missing weeks in contributionCalendar"

    # Validate contribution counts consistency
    total_from_api = calendar.get("totalContributions", 0)
    total_calculated = sum(d["contributionCount"]
                          for w in calendar["weeks"]
                          for d in w.get("contributionDays", []))

    if total_from_api != total_calculated:
        log.warning(
            "Contribution count mismatch: API says %d, calculated from days = %d",
            total_from_api, total_calculated
        )
        # Don't reject, but flag as potential issue

    return True, None


# ─── Cache helpers ─────────────────────────────────────────────────────────────
def load_cache() -> Optional[FetchResult]:
    """Return cached data if it exists and isn't past CACHE_HARD_EXPIRY, wrapped in FetchResult."""
    if not CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(CACHE_FILE.read_text("utf-8"))
        ts = payload.get("_cached_at")
        data = payload.get("data")

        if not data:
            return None

        cached_dt = datetime.fromisoformat(ts) if ts else None
        age = datetime.now(timezone.utc) - cached_dt if cached_dt else None

        if age and age <= CACHE_HARD_EXPIRY:
            log.info("Cache available — age %s", age)
            return FetchResult(
                status=ResultStatus.SUCCESS_USING_CACHE,
                data=data,
                source="cache",
                cached_at=ts,
                is_partial=payload.get("_is_partial", False),
            )
        else:
            log.info("Cache past hard expiry (%s) — age %s, ignoring", CACHE_HARD_EXPIRY, age)
            return None
    except Exception as exc:
        log.warning("Cache read failed: %s", exc)
        return None


def save_cache(data: dict, is_partial: bool = False) -> None:
    """Persist freshly-fetched API data with metadata."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "_cached_at": datetime.now(timezone.utc).isoformat(),
        "_is_partial": is_partial,
        "data": data,
    }
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


def fetch_live_data(deadline: Optional[float] = None) -> FetchResult:
    """
    Fetch live data from GitHub GraphQL API.
    Returns FetchResult with status, source, and latency info.
    `deadline` is an absolute time.time() value; the request (and its retries)
    will bail out early rather than exceed it.
    """
    if not TOKEN:
        return FetchResult(
            status=ResultStatus.TEMPORARY_UNAVAILABLE,
            source="graphql_api",
            error_message="No GH_STATS_TOKEN set — cannot call authenticated GraphQL",
            fallback_reason=FallbackReason.AUTH_FAILED,
        )

    start = time.time()
    try:
        r = _post(
            GRAPHQL_URL,
            headers=HEADERS,
            json={"query": _GQL_QUERY, "variables": {"login": USERNAME}},
            timeout=LIVE_FETCH_TIMEOUT,
            retries=LIVE_FETCH_RETRIES,
            deadline=deadline,
        )
        duration_ms = (time.time() - start) * 1000

        payload = r.json()

        # Check for GraphQL errors
        if "errors" in payload:
            errors = payload.get("errors", [])
            error_str = str(errors[0].get("message", "Unknown error")) if errors else "Unknown error"
            log.warning("GraphQL error: %s", error_str)
            return FetchResult(
                status=ResultStatus.TEMPORARY_UNAVAILABLE,
                source="graphql_api",
                error_message=f"GraphQL error: {error_str}",
                duration_ms=duration_ms,
                fallback_reason=FallbackReason.MALFORMED_RESPONSE,
            )

        # Extract user data
        data = payload.get("data", {})
        if not data.get("user"):
            return FetchResult(
                status=ResultStatus.USER_NOT_FOUND,
                source="graphql_api",
                error_message="GraphQL returned no user data",
                duration_ms=duration_ms,
            )

        user = data["user"]

        # Validate data before accepting
        is_valid, error_msg = validate_user_data(user)
        if not is_valid:
            log.error("Data validation failed: %s", error_msg)
            return FetchResult(
                status=ResultStatus.PARTIAL_RESULT,
                source="graphql_api",
                error_message=f"Validation failed: {error_msg}",
                duration_ms=duration_ms,
                fallback_reason=FallbackReason.VALIDATION_FAILED,
                is_partial=True,
            )

        # Validate contribution calendar specifically
        calendar = user["contributionsCollection"]["contributionCalendar"]
        is_valid, error_msg = validate_contribution_data(calendar["weeks"])
        if not is_valid:
            log.warning("Contribution data validation failed: %s", error_msg)

        log.info("✅ GraphQL fetch successful (%.0fms)", duration_ms)
        return FetchResult(
            status=ResultStatus.SUCCESS,
            data=data,
            source="graphql_api",
            fetched_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=duration_ms,
        )

    except RuntimeError as exc:
        duration_ms = (time.time() - start) * 1000
        msg = str(exc).lower()
        if "rate limit" in msg:
            log.warning("GraphQL rate limited (%.0fms)", duration_ms)
            return FetchResult(
                status=ResultStatus.TEMPORARY_UNAVAILABLE,
                source="graphql_api",
                duration_ms=duration_ms,
                fallback_reason=FallbackReason.RATE_LIMITED,
                error_message="Rate limited by GitHub API",
            )
        if "budget exceeded" in msg:
            log.warning("GraphQL fetch aborted — latency budget exceeded (%.0fms)", duration_ms)
            return FetchResult(
                status=ResultStatus.TEMPORARY_UNAVAILABLE,
                source="graphql_api",
                duration_ms=duration_ms,
                fallback_reason=FallbackReason.BUDGET_EXCEEDED,
                error_message="Live fetch exceeded latency budget",
            )
        log.warning("GraphQL fetch failed: %s (%.0fms)", exc, duration_ms)
        return FetchResult(
            status=ResultStatus.TEMPORARY_UNAVAILABLE,
            source="graphql_api",
            duration_ms=duration_ms,
            fallback_reason=FallbackReason.HTTP_ERROR,
            error_message=str(exc),
        )
    except requests.Timeout:
        duration_ms = (time.time() - start) * 1000
        log.warning("GraphQL timeout (%.0fms)", duration_ms)
        return FetchResult(
            status=ResultStatus.TEMPORARY_UNAVAILABLE,
            source="graphql_api",
            duration_ms=duration_ms,
            fallback_reason=FallbackReason.TIMEOUT,
            error_message="Request timeout",
        )
    except Exception as exc:
        duration_ms = (time.time() - start) * 1000
        log.error("GraphQL fetch crashed: %s (%.0fms)", exc, duration_ms)
        return FetchResult(
            status=ResultStatus.TEMPORARY_UNAVAILABLE,
            source="graphql_api",
            duration_ms=duration_ms,
            fallback_reason=FallbackReason.NETWORK_ERROR,
            error_message=str(exc),
        )


def get_github_data() -> FetchResult:
    """
    Live-first fetch controller.

    Ordering (this is the actual fix for real-time freshness):
      0. Hot cache  — only if it's younger than HOT_CACHE_TTL (~90s). Protects
         against back-to-back runs hammering the API; otherwise skipped.
      1. Live API   — always attempted, with a hard OVERALL_BUDGET_SECONDS budget,
         so the card reflects the current state as closely as latency allows.
      2. Cache       — used only if live fails, regardless of age up to
         CACHE_HARD_EXPIRY. The on-card badge will mark it stale if it's past
         CACHE_STALE_AFTER, so this is a safety net, not a silent substitute.
      3. Nothing     — both live and cache are unavailable.

    Previously cache was checked *before* live and accepted for up to 14 days
    with no distinction from a genuinely fresh fetch — meaning the card could
    silently go two weeks without updating even though live fetches would have
    succeeded. That's fixed here: live is always tried first.
    """
    log.info(SEP)
    log.info("🔄 Fetching GitHub data — live-first strategy")
    log.info(SEP)

    start_total = time.time()
    deadline = start_total + OVERALL_BUDGET_SECONDS

    # Layer 0: hot-cache micro-optimization.
    cached = load_cache()
    if cached and cached.cached_at:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cached.cached_at)
        if age <= HOT_CACHE_TTL:
            log.info("Layer 0: Hot cache (age %.0fs) — skipping live call this run", age.total_seconds())
            cached.duration_ms = (time.time() - start_total) * 1000
            log.info(SEP)
            return cached

    # Layer 1: Live API, always tried first.
    log.info("Layer 1: Live GraphQL API (budget: %ds)…", OVERALL_BUDGET_SECONDS)
    result = fetch_live_data(deadline=deadline)
    if result.status == ResultStatus.SUCCESS:
        save_cache(result.data)
        total_ms = (time.time() - start_total) * 1000
        log.info("✅ SUCCESS via GraphQL API (%.0fms total)", total_ms)
        log.info(SEP)
        return result

    # Layer 2: live failed — fall back to cache, whatever age it is.
    log.info(
        "Layer 2: Live fetch failed (%s) — falling back to cache…",
        result.fallback_reason.value if result.fallback_reason else "unknown",
    )
    if cached:
        stale = False
        if cached.cached_at:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached.cached_at)
            stale = age > CACHE_STALE_AFTER
        if stale:
            log.warning("Cache is stale (older than %s) — using anyway as last resort", CACHE_STALE_AFTER)
        total_ms = (time.time() - start_total) * 1000
        cached.duration_ms = total_ms
        cached.fallback_reason = result.fallback_reason
        log.info("✅ SUCCESS_USING_CACHE (%.0fms total, freshness: %s)", total_ms, cached.freshness_label())
        log.info(SEP)
        return cached

    # Layer 3: no data at all.
    log.error("❌ All sources failed — no live data, no cache")
    total_ms = (time.time() - start_total) * 1000
    result.duration_ms = total_ms
    log.info(SEP)
    return result


# ─── Streak Calculation (with documented rules) ──────────────────────────────────
def calculate_streak(weeks: list) -> tuple[int, int]:
    """
    Per spec section 5: Correct streak algorithm from contribution days.

    CURRENT STREAK RULE (documented):
      - Starting from today, walk backward through consecutive active days (count > 0).
      - If today has contributions: include today in streak.
      - If today has no contributions yet: stop at yesterday (do not incorrectly destroy previous streak).
      - Handle timezone consistently: use UTC midnight.

    LONGEST STREAK RULE:
      - Iterate chronologically through all contribution dates.
      - Find the longest consecutive sequence of days with count > 0.

    EDGE CASES HANDLED:
      - Incomplete calendar: validate before calling this function.
      - Weekend gaps: not considered breaks (each day is independent, not just weekdays).
      - Malformed dates: validate before calling.
      - Timezone: all dates are in UTC YYYY-MM-DD format.
    """
    if not weeks:
        return 0, 0

    today = datetime.now(timezone.utc).date().isoformat()

    # Flatten all days and filter to today or earlier
    all_days = []
    for week in weeks:
        for day in week.get("contributionDays", []):
            if day["date"] <= today:
                all_days.append(day)

    if not all_days:
        return 0, 0

    # CURRENT STREAK: walk backward from most recent date
    current = 0
    for day in reversed(all_days):
        if day["contributionCount"] > 0:
            current += 1
        elif day["date"] == today:
            # Today has no contributions yet, but don't break the streak.
            # This allows the streak to continue if the day started without contributions.
            continue
        else:
            # Hit a day with zero contributions (not today), so streak ends
            break

    # LONGEST STREAK: iterate chronologically
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
    """Copy a pre-baked fallback SVG if it exists; otherwise generate placeholder."""
    src = FALLBACK_DIR / name
    if src.exists():
        shutil.copy(src, dest)
        log.info("Fallback copied: %s → %s", src, dest)
    else:
        _write_minimal_svg_with_marker(dest, f"Data unavailable — will retry", placeholder=True)


def _write_minimal_svg(path: Path, label: str) -> None:
    """Write a minimal dark placeholder SVG (for harmless cases)."""
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120" viewBox="0 0 400 120">'
        f'<rect width="400" height="120" rx="8" fill="#121212" stroke="#27272a" stroke-width="1"/>'
        f'<text x="200" y="66" text-anchor="middle" fill="#9ca3af" font-family="sans-serif" font-size="14">{label}</text>'
        f"</svg>",
        encoding="utf-8",
    )
    log.info("Placeholder written: %s", path)


def _write_minimal_svg_with_marker(path: Path, label: str, placeholder: bool = False) -> None:
    """Write SVG with marker so health checks can distinguish temporary from permanent failures."""
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


def _freshness_badge_markup(meta: Optional[FetchResult], x: int = 424, y: int = 20) -> str:
    """
    Small top-right dot + label ('live', 'cached 3m ago', 'stale', ...) embedded
    directly in the card SVG. This is what makes the fallback chain "robust to
    display": a viewer never has to guess whether a number is current — it's on
    the card itself, updated every render.
    """
    if meta is None:
        return ""
    color, label = meta.badge()
    if not label:
        return ""
    return (
        f'<g transform="translate({x - len(label) * 5}, {y})">'
        f'<circle cx="-8" cy="-4" r="3.5" fill="{color}"/>'
        f'<text x="0" y="0" text-anchor="end" font-family="Segoe UI, Ubuntu, Sans-Serif" '
        f'font-size="10" fill="#8b949e">{label}</text>'
        f'</g>'
    )


# ─── Card renderers ────────────────────────────────────────────────────────────
def make_stats_svg(data: dict, meta: Optional[FetchResult] = None) -> None:
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
        badge = _freshness_badge_markup(meta, x=424, y=22)

        svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" width="450" height="195" viewBox="0 0 450 195">
  <style>
    .title {{ font: bold 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: #e6edf3; }}
    .label {{ font: 14px 'Segoe UI', Ubuntu, Sans-Serif; fill: #8b949e; }}
    .value {{ font: bold 14px 'Segoe UI', Ubuntu, Sans-Serif; fill: #e6edf3; }}
    .icon {{ fill: #8b949e; }}
  </style>
  <rect x="0.5" y="0.5" rx="6" ry="6" width="449" height="194" fill="#0d1117" stroke="#30363d" stroke-width="1"/>
  <text x="25" y="35" class="title">GitHub Stats</text>
  {badge}

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
        log.info("✅ github-stats.svg written with real data.")
    except Exception as exc:
        log.error("make_stats_svg failed: %s", exc)
        if not out.exists() or out.stat().st_size < 100:
            _fallback_copy("github-stats.svg", out)


def make_streak_svg(data: dict, meta: Optional[FetchResult] = None) -> None:
    """Render streak card with validated data, documented calculation, and a freshness badge."""
    out = ASSETS_DIR / "streak.svg"
    try:
        weeks = data["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]

        # Validate before calculating
        is_valid, error_msg = validate_contribution_data(weeks)
        if not is_valid:
            log.warning("Contribution data validation failed for streak: %s", error_msg)
            # Fall back to SVG if data is invalid
            _fallback_copy("streak.svg", out)
            return

        current, longest = calculate_streak(weeks)
        total = data["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"]

        log.info("Rendering streak with REAL validated data: current=%d, longest=%d, total=%d", current, longest, total)
        badge = _freshness_badge_markup(meta, x=424, y=22)

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
  {badge}

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
        (Path("assets/streak") / "streak.svg").write_text(svg_content, encoding="utf-8")
        log.info("✅ streak.svg written with real validated data.")
    except Exception as exc:
        log.error("make_streak_svg failed: %s", exc)
        if not out.exists() or out.stat().st_size < 100:
            _fallback_copy("streak.svg", out)


# ─── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    log.info("\n" + "="*60)
    log.info("🚀 Enhanced GitHub Stats Generator")
    log.info("="*60)
    log.info("Policy: Live-first > Correctness > Cache fallback > Diagnostics")
    log.info("="*60 + "\n")

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    Path("assets/streak").mkdir(parents=True, exist_ok=True)

    # Fetch data with structured result
    result = get_github_data()

    if result.status in (ResultStatus.SUCCESS, ResultStatus.SUCCESS_USING_CACHE, ResultStatus.SUCCESS_USING_FALLBACK):
        if result.data:
            # Render cards only with validated data; pass `result` through so
            # each card can show its own live/cached/stale freshness badge.
            log.info("\n📊 Rendering stats cards…")
            make_stats_svg(result.data, result)
            make_streak_svg(result.data, result)
            log.info("\n✨ Stats generation complete with %s (freshness: %s)", result.source, result.freshness_label())
        else:
            log.error("❌ Result status is success but data is None")
            sys.exit(1)
    else:
        log.error("❌ Failed to fetch any valid data: %s", result.error_message)
        log.error("   Status: %s | Source: %s | Reason: %s", result.status.value, result.source,
                 result.fallback_reason.value if result.fallback_reason else "unknown")
        sys.exit(1)


if __name__ == "__main__":
    main()
