"""
generate_stats.py

GitHub Stats + Contribution Streak Generator
---------------------------------------------

Design goals:
    1. Fresh GitHub streak data
    2. Correct date-based streak calculation
    3. Safe fallback to last-known-good cache
    4. No silent hot-cache masking of new contributions
    5. Fast critical GraphQL request
    6. Explicit source/freshness diagnostics
    7. Atomic cache writes
    8. Deterministic streak calculation for testing

Environment:
    GH_USERNAME      GitHub username
    GH_STATS_TOKEN   GitHub token with appropriate permissions

Output:
    assets/stats/github-stats.svg
    assets/stats/streak.svg
    assets/streak/streak.svg
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import requests


# ============================================================================
# CONFIGURATION
# ============================================================================

USERNAME = os.getenv("GH_USERNAME", "BhattAyush17")
TOKEN = os.getenv("GH_STATS_TOKEN", "")

GRAPHQL_URL = "https://api.github.com/graphql"

ASSETS_DIR = Path("assets/stats")
STREAK_DIR = Path("assets/streak")
CACHE_DIR = Path("assets/cache")

CACHE_SCHEMA_VERSION = 2
CACHE_FILE = CACHE_DIR / f"{USERNAME.lower()}-github.json"

# IMPORTANT:
# Do NOT silently use cache for normal runs.
# A newly created contribution must trigger a live GitHub request.
USE_HOT_CACHE = False

# Cache is only a safety net if GitHub cannot be reached.
CACHE_HARD_EXPIRY = timedelta(days=14)
CACHE_STALE_AFTER = timedelta(hours=6)

# Hard wall-clock budget for the live request.
OVERALL_BUDGET_SECONDS = 8.0

# Maximum socket timeout.
MAX_REQUEST_TIMEOUT = 7.0

# Only retry transient network/server failures.
MAX_RETRIES = 1

BACKOFF_SECONDS = 0.5

# GitHub contribution data should be treated as date-based.
# UTC is used consistently so the algorithm is deterministic.
REFERENCE_TZ = timezone.utc


# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("github-stats")


# ============================================================================
# RESULT TYPES
# ============================================================================

class ResultStatus(Enum):
    SUCCESS = "SUCCESS"
    SUCCESS_USING_CACHE = "SUCCESS_USING_CACHE"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    TEMPORARY_UNAVAILABLE = "TEMPORARY_UNAVAILABLE"
    NO_DATA = "NO_DATA"


class FailureReason(Enum):
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILED = "AUTH_FAILED"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    INVALID_CALENDAR = "INVALID_CALENDAR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


@dataclass
class FetchResult:
    status: ResultStatus
    data: Optional[dict[str, Any]] = None

    source: str = "unknown"

    fetched_at: Optional[str] = None
    cached_at: Optional[str] = None

    duration_ms: float = 0.0

    failure_reason: Optional[FailureReason] = None
    error_message: Optional[str] = None

    is_partial: bool = False

    @property
    def data_timestamp(self) -> Optional[str]:
        """
        Timestamp describing the actual data shown to the user.
        """
        if self.status == ResultStatus.SUCCESS:
            return self.fetched_at

        return self.cached_at

    def data_age(self) -> Optional[timedelta]:
        ts = self.data_timestamp

        if not ts:
            return None

        try:
            parsed = datetime.fromisoformat(ts)

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return datetime.now(timezone.utc) - parsed

        except Exception:
            return None

    def freshness_label(self) -> str:
        age = self.data_age()

        if age is None:
            return "unknown"

        seconds = max(0, int(age.total_seconds()))

        if seconds < 60:
            return "just now"

        minutes = seconds // 60

        if minutes < 60:
            return f"{minutes} min ago"

        hours = minutes // 60

        if hours < 24:
            return f"{hours}h ago"

        return f"{hours // 24}d ago"

    def badge(self) -> tuple[str, str]:

        if self.status == ResultStatus.SUCCESS:
            return "#3fb950", "live"

        if self.status == ResultStatus.SUCCESS_USING_CACHE:
            age = self.data_age()

            if age and age > CACHE_STALE_AFTER:
                return "#f85149", f"stale · {self.freshness_label()}"

            return "#d29922", f"cached · {self.freshness_label()}"

        return "#8b949e", "unavailable"


# ============================================================================
# HTTP SESSION
# ============================================================================

SESSION = requests.Session()

SESSION.headers.update(
    {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "github-stats-generator",
    }
)

if TOKEN:
    SESSION.headers.update(
        {
            "Authorization": f"Bearer {TOKEN}",
        }
    )
else:
    log.warning(
        "GH_STATS_TOKEN is not set. "
        "Authenticated GraphQL requests will not be available."
    )


# ============================================================================
# TIME HELPERS
# ============================================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def reference_date() -> date:
    return now_utc().date()


# ============================================================================
# GRAPHQL QUERY
# ============================================================================

# IMPORTANT:
# This query is deliberately small.
#
# The streak pipeline does NOT wait for repositories, followers, stars,
# languages, descriptions, etc.
#
# That makes contribution retrieval the critical fast path.

STREAK_GRAPHQL_QUERY = """
query($login: String!) {
    user(login: $login) {
        login

        contributionsCollection {
            contributionCalendar {
                totalContributions
                weeks {
                    contributionDays {
                        date
                        contributionCount
                    }
                }
            }

            totalCommitContributions
            totalPullRequestContributions
            totalIssueContributions
            restrictedContributionsCount
        }
    }
}
"""


# Secondary query for the normal stats card.
# This is separated so streak retrieval isn't slowed by repository metadata.

STATS_GRAPHQL_QUERY = """
query($login: String!) {
    user(login: $login) {

        repositories(
            first: 100
            ownerAffiliations: OWNER
            privacy: PUBLIC
            orderBy: {field: PUSHED_AT, direction: DESC}
        ) {
            nodes {
                name
                stargazerCount
                forkCount
                primaryLanguage {
                    name
                    color
                }
                pushedAt
                description
                url
            }
        }

        followers {
            totalCount
        }

        contributionsCollection {
            totalCommitContributions
            restrictedContributionsCount
            totalPullRequestContributions
            totalIssueContributions
        }
    }
}
"""


# ============================================================================
# HTTP ERROR CLASSIFICATION
# ============================================================================

def classify_http_failure(response: requests.Response) -> Optional[FailureReason]:
    status = response.status_code

    if status == 401:
        return FailureReason.AUTH_FAILED

    if status == 404:
        return FailureReason.USER_NOT_FOUND

    if status == 429:
        return FailureReason.RATE_LIMITED

    if status == 403:

        remaining = response.headers.get(
            "X-RateLimit-Remaining"
        )

        if remaining == "0":
            return FailureReason.RATE_LIMITED

        # GitHub can return 403 for reasons other than rate limiting.
        return FailureReason.INVALID_RESPONSE

    if 500 <= status <= 599:
        return FailureReason.SERVER_ERROR

    return FailureReason.INVALID_RESPONSE


# ============================================================================
# GRAPHQL FETCH
# ============================================================================

def graphql_request(
    query: str,
    deadline: float,
) -> FetchResult:

    start = time.monotonic()

    if not TOKEN:
        return FetchResult(
            status=ResultStatus.TEMPORARY_UNAVAILABLE,
            source="graphql_api",
            failure_reason=FailureReason.AUTH_FAILED,
            error_message="GH_STATS_TOKEN is not configured",
        )

    for attempt in range(MAX_RETRIES + 1):

        remaining = deadline - time.monotonic()

        if remaining <= 0:
            return FetchResult(
                status=ResultStatus.TEMPORARY_UNAVAILABLE,
                source="graphql_api",
                failure_reason=FailureReason.BUDGET_EXCEEDED,
                error_message="Live request budget exceeded",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        timeout = min(
            MAX_REQUEST_TIMEOUT,
            max(0.5, remaining - 0.1),
        )

        try:

            response = SESSION.post(
                GRAPHQL_URL,
                json={
                    "query": query,
                    "variables": {
                        "login": USERNAME,
                    },
                },
                timeout=timeout,
            )

            duration_ms = (time.monotonic() - start) * 1000

            failure = classify_http_failure(response)

            if failure:
                return FetchResult(
                    status=(
                        ResultStatus.USER_NOT_FOUND
                        if failure == FailureReason.USER_NOT_FOUND
                        else ResultStatus.TEMPORARY_UNAVAILABLE
                    ),
                    source="graphql_api",
                    failure_reason=failure,
                    error_message=f"GitHub HTTP {response.status_code}",
                    duration_ms=duration_ms,
                )

            try:
                payload = response.json()

            except ValueError:
                return FetchResult(
                    status=ResultStatus.TEMPORARY_UNAVAILABLE,
                    source="graphql_api",
                    failure_reason=FailureReason.INVALID_RESPONSE,
                    error_message="GitHub returned invalid JSON",
                    duration_ms=duration_ms,
                )

            # GraphQL can return HTTP 200 with errors.
            errors = payload.get("errors")

            if errors:

                message = (
                    errors[0].get("message", "Unknown GraphQL error")
                    if isinstance(errors, list) and errors
                    else "Unknown GraphQL error"
                )

                return FetchResult(
                    status=ResultStatus.TEMPORARY_UNAVAILABLE,
                    source="graphql_api",
                    failure_reason=FailureReason.INVALID_RESPONSE,
                    error_message=message,
                    duration_ms=duration_ms,
                )

            data = payload.get("data")

            if not isinstance(data, dict):
                return FetchResult(
                    status=ResultStatus.TEMPORARY_UNAVAILABLE,
                    source="graphql_api",
                    failure_reason=FailureReason.INVALID_RESPONSE,
                    error_message="Missing GraphQL data",
                    duration_ms=duration_ms,
                )

            user = data.get("user")

            if not user:
                return FetchResult(
                    status=ResultStatus.USER_NOT_FOUND,
                    source="graphql_api",
                    failure_reason=FailureReason.USER_NOT_FOUND,
                    error_message=f"GitHub user '{USERNAME}' not found",
                    duration_ms=duration_ms,
                )

            return FetchResult(
                status=ResultStatus.SUCCESS,
                data=data,
                source="graphql_api",
                fetched_at=now_iso(),
                duration_ms=duration_ms,
            )

        except requests.Timeout:

            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_SECONDS)
                continue

            return FetchResult(
                status=ResultStatus.TEMPORARY_UNAVAILABLE,
                source="graphql_api",
                failure_reason=FailureReason.TIMEOUT,
                error_message="GitHub request timed out",
                duration_ms=(time.monotonic() - start) * 1000,
            )

        except requests.RequestException as exc:

            if attempt < MAX_RETRIES:

                remaining = deadline - time.monotonic()

                if remaining > BACKOFF_SECONDS:
                    time.sleep(BACKOFF_SECONDS)
                    continue

            return FetchResult(
                status=ResultStatus.TEMPORARY_UNAVAILABLE,
                source="graphql_api",
                failure_reason=FailureReason.NETWORK_ERROR,
                error_message=str(exc),
                duration_ms=(time.monotonic() - start) * 1000,
            )

    return FetchResult(
        status=ResultStatus.TEMPORARY_UNAVAILABLE,
        source="graphql_api",
        failure_reason=FailureReason.NETWORK_ERROR,
        error_message="Unknown network failure",
        duration_ms=(time.monotonic() - start) * 1000,
    )


# ============================================================================
# CONTRIBUTION VALIDATION
# ============================================================================

def validate_calendar(
    weeks: Any,
) -> tuple[bool, bool, Optional[str]]:

    """
    Returns:

        valid
        partial
        error_message
    """

    if not isinstance(weeks, list) or not weeks:
        return False, True, "Contribution calendar is empty"

    seen: set[str] = set()

    valid_days = 0

    last_date: Optional[date] = None

    today = reference_date()

    for week_index, week in enumerate(weeks):

        if not isinstance(week, dict):
            return False, True, f"Week {week_index} is invalid"

        days = week.get("contributionDays")

        if not isinstance(days, list):
            return False, True, (
                f"Week {week_index} has no contributionDays"
            )

        for day_index, day_data in enumerate(days):

            if not isinstance(day_data, dict):
                return False, True, (
                    f"Day {week_index}/{day_index} is invalid"
                )

            raw_date = day_data.get("date")
            raw_count = day_data.get("contributionCount")

            if not isinstance(raw_date, str):
                return False, True, "Contribution day has no date"

            if not isinstance(raw_count, int):
                return False, True, (
                    f"Invalid contribution count for {raw_date}"
                )

            if raw_count < 0:
                return False, True, (
                    f"Negative contribution count for {raw_date}"
                )

            try:
                parsed_date = date.fromisoformat(raw_date)

            except ValueError:
                return False, True, (
                    f"Invalid ISO date: {raw_date}"
                )

            if parsed_date > today:
                return False, True, (
                    f"Future contribution date: {raw_date}"
                )

            if raw_date in seen:
                return False, True, (
                    f"Duplicate contribution date: {raw_date}"
                )

            seen.add(raw_date)

            if last_date and parsed_date < last_date:
                return False, True, (
                    f"Contribution dates are unordered: "
                    f"{last_date} -> {parsed_date}"
                )

            last_date = parsed_date
            valid_days += 1

    if valid_days == 0:
        return False, True, "No contribution days found"

    # GitHub's visible contribution calendar is approximately one year.
    # Treat obviously short calendars as partial.
    partial = len(weeks) < 50

    if partial:
        log.warning(
            "Contribution calendar contains only %d weeks",
            len(weeks),
        )

    return True, partial, None


# ============================================================================
# CONTRIBUTION MAP
# ============================================================================

def build_date_map(
    weeks: list[dict[str, Any]],
) -> dict[date, int]:

    date_map: dict[date, int] = {}

    for week in weeks:

        for day_data in week.get("contributionDays", []):

            parsed = date.fromisoformat(
                day_data["date"]
            )

            date_map[parsed] = int(
                day_data["contributionCount"]
            )

    return date_map


# ============================================================================
# STREAK CALCULATION
# ============================================================================

def calculate_streaks(
    weeks: list[dict[str, Any]],
    reference: date,
) -> dict[str, Any]:

    """
    Deterministic contribution calculation.

    Current streak:

        If today is active:
            start today.

        If today is inactive:
            start yesterday.

        Then walk backward using actual calendar dates.

    A missing date is NOT considered active.

    Longest streak:

        Walk through sorted active dates and require exact
        one-day adjacency.
    """

    date_map = build_date_map(weeks)

    if not date_map:
        return {
            "current_streak": 0,
            "longest_streak": 0,
            "active_days": 0,
            "last_contribution_date": None,
        }

    active_dates = sorted(
        d
        for d, count in date_map.items()
        if count > 0 and d <= reference
    )

    active_days = len(active_dates)

    if not active_dates:

        return {
            "current_streak": 0,
            "longest_streak": 0,
            "active_days": 0,
            "last_contribution_date": None,
        }

    # ---------------------------------------------------------------------
    # CURRENT STREAK
    # ---------------------------------------------------------------------

    if date_map.get(reference, 0) > 0:

        cursor = reference

    else:

        cursor = reference - timedelta(days=1)

    current_streak = 0

    while date_map.get(cursor, 0) > 0:

        current_streak += 1
        cursor -= timedelta(days=1)

    # ---------------------------------------------------------------------
    # LONGEST STREAK
    # ---------------------------------------------------------------------

    longest = 0
    running = 0
    previous: Optional[date] = None

    for current in active_dates:

        if (
            previous is not None
            and current == previous + timedelta(days=1)
        ):
            running += 1

        else:
            running = 1

        longest = max(longest, running)
        previous = current

    # ---------------------------------------------------------------------
    # OTHER STATS
    # ---------------------------------------------------------------------

    total_contributions = sum(
        count
        for d, count in date_map.items()
        if d <= reference
    )

    current_year = reference.year

    current_year_contributions = sum(
        count
        for d, count in date_map.items()
        if d.year == current_year
        and d <= reference
    )

    last_contribution_date = active_dates[-1]

    return {
        "current_streak": current_streak,
        "longest_streak": longest,
        "active_days": active_days,
        "total_contributions": total_contributions,
        "current_year_contributions": current_year_contributions,
        "last_contribution_date": last_contribution_date.isoformat(),
    }


# ============================================================================
# NORMALIZE GITHUB DATA
# ============================================================================

def normalize_contribution_data(
    raw_data: dict[str, Any],
) -> tuple[bool, dict[str, Any], Optional[str]]:

    user = raw_data.get("user")

    if not isinstance(user, dict):
        return False, {}, "Missing GitHub user"

    cc = user.get("contributionsCollection")

    if not isinstance(cc, dict):
        return False, {}, "Missing contributionsCollection"

    calendar = cc.get("contributionCalendar")

    if not isinstance(calendar, dict):
        return False, {}, "Missing contributionCalendar"

    weeks = calendar.get("weeks")

    valid, partial, error = validate_calendar(weeks)

    if not valid:
        return False, {}, error

    stats = calculate_streaks(
        weeks,
        reference_date(),
    )

    normalized = {
        "username": user.get("login", USERNAME),

        "weeks": weeks,

        "github_total_contributions": calendar.get(
            "totalContributions",
            0,
        ),

        "total_commit_contributions": cc.get(
            "totalCommitContributions",
            0,
        ),

        "total_pull_request_contributions": cc.get(
            "totalPullRequestContributions",
            0,
        ),

        "total_issue_contributions": cc.get(
            "totalIssueContributions",
            0,
        ),

        "restricted_contributions": cc.get(
            "restrictedContributionsCount",
            0,
        ),

        **stats,

        "_is_partial": partial,
    }

    return True, normalized, None


# ============================================================================
# CACHE
# ============================================================================

def load_cache() -> Optional[FetchResult]:

    if not CACHE_FILE.exists():
        return None

    try:

        payload = json.loads(
            CACHE_FILE.read_text(
                encoding="utf-8"
            )
        )

        if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
            log.warning("Ignoring incompatible cache schema")
            return None

        cached_at = payload.get("cached_at")
        data = payload.get("data")

        if not cached_at or not data:
            return None

        cached_time = datetime.fromisoformat(cached_at)

        if cached_time.tzinfo is None:
            cached_time = cached_time.replace(
                tzinfo=timezone.utc
            )

        age = now_utc() - cached_time

        if age > CACHE_HARD_EXPIRY:
            log.warning(
                "Cache expired: %s old",
                age,
            )
            return None

        return FetchResult(
            status=ResultStatus.SUCCESS_USING_CACHE,
            data=data,
            source="cache",
            cached_at=cached_at,
            is_partial=bool(
                payload.get(
                    "is_partial",
                    False,
                )
            ),
        )

    except Exception as exc:

        log.warning(
            "Cache could not be loaded: %s",
            exc,
        )

        return None


def save_cache(
    data: dict[str, Any],
) -> None:

    CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "username": USERNAME,
        "cached_at": now_iso(),
        "is_partial": bool(
            data.get("_is_partial", False)
        ),
        "data": data,
    }

    try:

        CACHE_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        fd, temporary_path = tempfile.mkstemp(
            prefix=".github-cache-",
            suffix=".tmp",
            dir=str(CACHE_DIR),
        )

        try:

            with os.fdopen(
                fd,
                "w",
                encoding="utf-8",
            ) as handle:

                json.dump(
                    payload,
                    handle,
                    indent=2,
                )

                handle.flush()
                os.fsync(handle.fileno())

            os.replace(
                temporary_path,
                CACHE_FILE,
            )

        finally:

            if os.path.exists(
                temporary_path
            ):
                os.unlink(
                    temporary_path
                )

        log.info(
            "Cache updated: %s",
            CACHE_FILE,
        )

    except Exception as exc:

        log.warning(
            "Cache write failed: %s",
            exc,
        )


# ============================================================================
# MAIN DATA PIPELINE
# ============================================================================

def get_github_data() -> FetchResult:

    log.info("=" * 60)
    log.info("GitHub contribution fetch")
    log.info("User: %s", USERNAME)
    log.info("=" * 60)

    start = time.monotonic()

    # ---------------------------------------------------------------------
    # IMPORTANT:
    #
    # We intentionally DO NOT return a hot cache result.
    #
    # This is the main change responsible for making new contributions
    # visible on the next run.
    # ---------------------------------------------------------------------

    cache = load_cache()

    deadline = (
        start
        + OVERALL_BUDGET_SECONDS
    )

    log.info(
        "Requesting LIVE contribution calendar..."
    )

    live = graphql_request(
        STREAK_GRAPHQL_QUERY,
        deadline,
    )

    if live.status == ResultStatus.SUCCESS:

        assert live.data is not None

        valid, normalized, error = (
            normalize_contribution_data(
                live.data
            )
        )

        if not valid:

            log.error(
                "Live contribution data invalid: %s",
                error,
            )

        else:

            save_cache(normalized)

            total_ms = (
                time.monotonic() - start
            ) * 1000

            live.data = normalized
            live.duration_ms = total_ms
            live.is_partial = bool(
                normalized.get(
                    "_is_partial",
                    False,
                )
            )

            log.info(
                "LIVE SUCCESS: %.0f ms",
                total_ms,
            )

            log.info(
                "Current streak: %d",
                normalized[
                    "current_streak"
                ],
            )

            log.info(
                "Longest streak: %d",
                normalized[
                    "longest_streak"
                ],
            )

            log.info(
                "Last contribution: %s",
                normalized[
                    "last_contribution_date"
                ],
            )

            return live

    # ---------------------------------------------------------------------
    # CACHE FALLBACK
    # ---------------------------------------------------------------------

    reason = (
        live.failure_reason.value
        if live.failure_reason
        else "unknown"
    )

    log.warning(
        "Live GitHub request failed: %s",
        reason,
    )

    if cache:

        cache.duration_ms = (
            time.monotonic() - start
        ) * 1000

        cache.failure_reason = (
            live.failure_reason
        )

        log.warning(
            "Using last-known-good cache: %s",
            cache.freshness_label(),
        )

        return cache

    total_ms = (
        time.monotonic() - start
    ) * 1000

    live.duration_ms = total_ms

    return live


# ============================================================================
# FRESHNESS BADGE
# ============================================================================

def freshness_badge(
    meta: FetchResult,
) -> str:

    color, label = meta.badge()

    return f"""
<g transform="translate(420,22)">
    <circle cx="-10" cy="-4" r="3.5" fill="{color}"/>
    <text
        x="0"
        y="0"
        text-anchor="end"
        font-family="Segoe UI, Ubuntu, Sans-Serif"
        font-size="10"
        fill="#8b949e"
    >{label}</text>
</g>
"""


# ============================================================================
# STREAK SVG
# ============================================================================

def make_streak_svg(
    result: FetchResult,
) -> None:

    output = STREAK_DIR / "streak.svg"
    output_alt = ASSETS_DIR / "streak.svg"

    STREAK_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    ASSETS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not result.data:

        write_unavailable_svg(
            output
        )

        write_unavailable_svg(
            output_alt
        )

        return

    data = result.data

    current = int(
        data.get(
            "current_streak",
            0,
        )
    )

    longest = int(
        data.get(
            "longest_streak",
            0,
        )
    )

    total = int(
        data.get(
            "github_total_contributions",
            data.get(
                "total_contributions",
                0,
            ),
        )
    )

    active_days = int(
        data.get(
            "active_days",
            0,
        )
    )

    current_year = int(
        data.get(
            "current_year_contributions",
            0,
        )
    )

    last_date = data.get(
        "last_contribution_date"
    )

    badge = freshness_badge(
        result
    )

    svg = f"""<svg
xmlns="http://www.w3.org/2000/svg"
width="450"
height="215"
viewBox="0 0 450 215">

<style>
.title {{
    font: bold 18px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.label {{
    font: 13px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #8b949e;
}}

.value {{
    font: bold 34px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.small-value {{
    font: bold 12px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.small-label {{
    font: 11px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #8b949e;
}}
</style>

<rect
    x="0.5"
    y="0.5"
    width="449"
    height="214"
    rx="7"
    fill="#0d1117"
    stroke="#30363d"
    stroke-width="1"
/>

{badge}

<text
    x="25"
    y="35"
    class="title"
>
    🔥 Current Streak
</text>

<text
    x="225"
    y="98"
    text-anchor="middle"
    class="value"
>
    {current}
</text>

<text
    x="225"
    y="120"
    text-anchor="middle"
    class="label"
>
    consecutive days
</text>

<line
    x1="25"
    y1="138"
    x2="425"
    y2="138"
    stroke="#27272a"
    stroke-width="1"
/>

<text x="25" y="158" class="small-label">
    Longest
</text>

<text x="25" y="176" class="small-value">
    {longest} days
</text>

<text x="145" y="158" class="small-label">
    Contributions
</text>

<text x="145" y="176" class="small-value">
    {total}
</text>

<text x="270" y="158" class="small-label">
    Active Days
</text>

<text x="270" y="176" class="small-value">
    {active_days}
</text>

<text x="25" y="198" class="small-label">
    {datetime.now().year} contributions: {current_year}
</text>

<text x="270" y="198" class="small-label">
    Last: {last_date or "none"}
</text>

</svg>
"""

    output.write_text(
        svg,
        encoding="utf-8",
    )

    output_alt.write_text(
        svg,
        encoding="utf-8",
    )

    log.info(
        "Streak SVG written: current=%d longest=%d",
        current,
        longest,
    )


# ============================================================================
# SIMPLE STATS SVG
# ============================================================================

def make_stats_svg(
    contribution_data: dict[str, Any],
    meta: FetchResult,
) -> None:

    output = ASSETS_DIR / "github-stats.svg"

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    commits = (
        contribution_data.get(
            "total_commit_contributions",
            0,
        )
        + contribution_data.get(
            "restricted_contributions",
            0,
        )
    )

    prs = contribution_data.get(
        "total_pull_request_contributions",
        0,
    )

    issues = contribution_data.get(
        "total_issue_contributions",
        0,
    )

    total = contribution_data.get(
        "github_total_contributions",
        0,
    )

    current = contribution_data.get(
        "current_streak",
        0,
    )

    longest = contribution_data.get(
        "longest_streak",
        0,
    )

    badge = freshness_badge(
        meta
    )

    svg = f"""<svg
xmlns="http://www.w3.org/2000/svg"
width="450"
height="195"
viewBox="0 0 450 195">

<style>
.title {{
    font: bold 18px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.label {{
    font: 14px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #8b949e;
}}

.value {{
    font: bold 14px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}
</style>

<rect
    x="0.5"
    y="0.5"
    width="449"
    height="194"
    rx="7"
    fill="#0d1117"
    stroke="#30363d"
/>

<text x="25" y="35" class="title">
    GitHub Stats
</text>

{badge}

<text x="25" y="65" class="label">
    Contributions
</text>

<text x="170" y="65" class="value">
    {total}
</text>

<text x="25" y="90" class="label">
    Commits
</text>

<text x="170" y="90" class="value">
    {commits}
</text>

<text x="25" y="115" class="label">
    Pull Requests
</text>

<text x="170" y="115" class="value">
    {prs}
</text>

<text x="25" y="140" class="label">
    Issues
</text>

<text x="170" y="140" class="value">
    {issues}
</text>

<text x="25" y="165" class="label">
    Current Streak
</text>

<text x="170" y="165" class="value">
    {current} days
</text>

</svg>
"""

    output.write_text(
        svg,
        encoding="utf-8",
    )


# ============================================================================
# UNAVAILABLE SVG
# ============================================================================

def write_unavailable_svg(
    path: Path,
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    svg = """<svg
xmlns="http://www.w3.org/2000/svg"
width="450"
height="180"
viewBox="0 0 450 180">

<rect
    x="0.5"
    y="0.5"
    width="449"
    height="179"
    rx="7"
    fill="#0d1117"
    stroke="#30363d"
/>

<text
    x="225"
    y="80"
    text-anchor="middle"
    fill="#e6edf3"
    font-size="18"
    font-family="Segoe UI, Ubuntu, Sans-Serif"
>
    GitHub data unavailable
</text>

<text
    x="225"
    y="110"
    text-anchor="middle"
    fill="#8b949e"
    font-size="12"
    font-family="Segoe UI, Ubuntu, Sans-Serif"
>
    Retry on the next update
</text>

</svg>
"""

    path.write_text(
        svg,
        encoding="utf-8",
    )


# ============================================================================
# TESTABLE STREAK FIXTURES
# ============================================================================

def _make_test_weeks(
    values: dict[str, int]
) -> list[dict[str, Any]]:

    dates = sorted(
        values.keys()
    )

    days = [
        {
            "date": d,
            "contributionCount": c,
        }
        for d, c in zip(
            dates,
            [values[d] for d in dates],
        )
    ]

    return [
        {
            "contributionDays": days
        }
    ]


def run_streak_self_test() -> None:

    base = date(2026, 8, 31)

    def fmt(offset: int) -> str:
        return (
            base - timedelta(
                days=offset
            )
        ).isoformat()

    # Today + previous two days.
    weeks = _make_test_weeks(
        {
            fmt(0): 1,
            fmt(1): 1,
            fmt(2): 1,
            fmt(3): 0,
        }
    )

    result = calculate_streaks(
        weeks,
        base,
    )

    assert result["current_streak"] == 3
    assert result["longest_streak"] == 3

    # Today zero, previous three active.
    weeks = _make_test_weeks(
        {
            fmt(0): 0,
            fmt(1): 1,
            fmt(2): 1,
            fmt(3): 1,
            fmt(4): 0,
        }
    )

    result = calculate_streaks(
        weeks,
        base,
    )

    assert result["current_streak"] == 3

    # Gap must break current streak.
    weeks = _make_test_weeks(
        {
            fmt(0): 1,
            fmt(1): 1,
            fmt(2): 0,
            fmt(3): 1,
        }
    )

    result = calculate_streaks(
        weeks,
        base,
    )

    assert result["current_streak"] == 2

    # Missing date must NOT count as active.
    weeks = _make_test_weeks(
        {
            fmt(0): 1,
            fmt(1): 1,
            fmt(3): 1,
        }
    )

    result = calculate_streaks(
        weeks,
        base,
    )

    assert result["current_streak"] == 2

    log.info(
        "✅ Streak self-tests passed"
    )


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:

    run_streak_self_test()

    result = get_github_data()

    if result.status not in (
        ResultStatus.SUCCESS,
        ResultStatus.SUCCESS_USING_CACHE,
    ):

        log.error(
            "GitHub statistics unavailable"
        )

        if result.failure_reason:
            log.error(
                "Reason: %s",
                result.failure_reason.value,
            )

        if result.error_message:
            log.error(
                "Details: %s",
                result.error_message,
            )

        sys.exit(1)

    if not result.data:
        log.error(
            "Result contains no data"
        )
        sys.exit(1)

    # The contribution data is already normalized.
    make_streak_svg(
        result
    )

    make_stats_svg(
        result.data,
        result,
    )

    log.info("=" * 60)
    log.info(
        "SOURCE: %s",
        result.source,
    )
    log.info(
        "FRESHNESS: %s",
        result.freshness_label(),
    )
    log.info(
        "CURRENT STREAK: %s",
        result.data.get(
            "current_streak"
        ),
    )
    log.info(
        "LONGEST STREAK: %s",
        result.data.get(
            "longest_streak"
        ),
    )
    log.info(
        "LAST CONTRIBUTION: %s",
        result.data.get(
            "last_contribution_date"
        ),
    )
    log.info(
        "LATENCY: %.0f ms",
        result.duration_ms,
    )
    log.info("=" * 60)


if __name__ == "__main__":
    main()
