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
    assets/stats/languages.svg
    assets/stats/languages-donut.svg
    assets/stats/streak.svg
    assets/streak/streak.svg
"""

from __future__ import annotations

import json
import logging
import math
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

# Separate budget for the secondary (non critical) request that feeds
# the languages card. The streak data must never wait for it.
SECONDARY_BUDGET_SECONDS = 6.0

# Every stats card uses the same canvas so the row lines up perfectly.
#
# 300px is deliberate. The README shows the three cards with a fixed
# width (not a percentage), so:
#
#   * desktop: 3 x 300px fits the README column, rendered 1:1
#   * mobile : 300px is narrower than the phone column, so each card is
#              still rendered 1:1 and simply stacks
#
# Nothing is scaled down, which is what used to make the text unreadable.
CARD_WIDTH = 300
CARD_HEIGHT = 210

# Maximum socket timeout.
MAX_REQUEST_TIMEOUT = 7.0

# Only retry transient network/server failures.
MAX_RETRIES = 1

BACKOFF_SECONDS = 0.5

# GitHub contribution data should be treated as date-based.
# UTC is used consistently so the algorithm is deterministic.
REFERENCE_TZ = timezone.utc

# GitHub buckets contributions by the USER's own timezone, not by UTC.
#
# That means the last day of the contribution calendar is not necessarily
# the UTC date:
#
#   * user ahead of UTC (e.g. UTC+05:30) -> last day is "tomorrow" in UTC
#   * user behind of UTC (e.g. UTC-07:00) -> last day is "yesterday" in UTC
#
# The calendar is therefore allowed to reach one day past the UTC date.
CALENDAR_FUTURE_TOLERANCE_DAYS = 1

# A calendar that is older than this is considered stale: it can no longer
# prove that the current streak is still alive, so the streak is reported
# as 0 instead of an outdated number.
CALENDAR_STALE_TOLERANCE_DAYS = 1


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

        # Monochrome dot: brightness carries the meaning instead of hue.
        if self.status == ResultStatus.SUCCESS:
            return "#e6edf3", "live"

        if self.status == ResultStatus.SUCCESS_USING_CACHE:
            age = self.data_age()

            if age and age > CACHE_STALE_AFTER:
                return "#484f58", f"stale · {self.freshness_label()}"

            return "#8b949e", f"cached · {self.freshness_label()}"

        return "#484f58", "unavailable"


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
    """
    Map an HTTP response to a failure reason.

    Returns None when the response is a success (2xx) so that callers
    can treat "no failure reason" as "the request worked".
    """

    status = response.status_code

    # A successful response is not a failure.
    # GraphQL answers with HTTP 200 even for logical errors, so this
    # branch must come before every other check.
    if 200 <= status < 300:
        return None

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
    utc_today: Optional[date] = None,
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

    today = utc_today or reference_date()

    # The calendar is rendered in the user's timezone, so its newest day
    # may legitimately be one day ahead of the UTC date.
    latest_allowed = today + timedelta(
        days=CALENDAR_FUTURE_TOLERANCE_DAYS
    )

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

            if parsed_date > latest_allowed:
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

def resolve_reference_day(
    date_map: dict[date, int],
    utc_today: date,
) -> date:
    """
    Resolve the user's current day from the contribution calendar.

    GitHub renders the contribution grid in the user's own timezone, so
    the newest day of the calendar is the user's "today":

        * user ahead of UTC  -> newest day is utc_today + 1
        * user behind of UTC -> newest day is utc_today - 1

    The newest day of the calendar is therefore used as the anchor for
    the current streak.

    A calendar that is clearly older than that is treated as stale: it
    cannot prove the streak is still alive, so the UTC date is used
    instead, which resolves the current streak to 0 rather than
    reporting an outdated number.
    """

    if not date_map:
        return utc_today

    newest_day = max(date_map)

    lag = (utc_today - newest_day).days

    if lag <= CALENDAR_STALE_TOLERANCE_DAYS:
        return newest_day

    log.warning(
        "Contribution calendar is stale: newest day %s is %d days "
        "behind %s - current streak cannot be trusted",
        newest_day,
        lag,
        utc_today,
    )

    return utc_today


def calculate_streaks(
    weeks: list[dict[str, Any]],
    reference: Optional[date] = None,
    utc_today: Optional[date] = None,
) -> dict[str, Any]:

    """
    Deterministic contribution calculation.

    Current streak:

        The anchor day is the user's current day, taken from the
        calendar itself (see resolve_reference_day).

        If the anchor day is active:
            start there.

        If the anchor day is inactive:
            start the day before.

        Then walk backward using actual calendar dates.

        A missing date is NOT considered active.

    Longest streak:

        Walk through sorted active dates and require exact
        one-day adjacency.

        Only the visible window (roughly one year) is covered, because
        that is all GitHub exposes.
    """

    date_map = build_date_map(weeks)

    today = utc_today or reference_date()

    if reference is None:
        reference = resolve_reference_day(date_map, today)

    if not date_map:
        return {
            "current_streak": 0,
            "current_streak_start": None,
            "longest_streak": 0,
            "active_days": 0,
            "total_contributions": 0,
            "current_year_contributions": 0,
            "reference_date": reference.isoformat(),
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
            "current_streak_start": None,
            "longest_streak": 0,
            "active_days": 0,
            "total_contributions": sum(
                count
                for d, count in date_map.items()
                if d <= reference
            ),
            "current_year_contributions": sum(
                count
                for d, count in date_map.items()
                if d.year == reference.year
                and d <= reference
            ),
            "reference_date": reference.isoformat(),
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

    streak_start: Optional[date] = None

    while date_map.get(cursor, 0) > 0:

        current_streak += 1
        streak_start = cursor
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
        "current_streak_start": (
            streak_start.isoformat()
            if streak_start is not None
            else None
        ),
        "longest_streak": longest,
        "active_days": active_days,
        "total_contributions": total_contributions,
        "current_year_contributions": current_year_contributions,
        "reference_date": reference.isoformat(),
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

    # The reference day is resolved from the calendar itself, because
    # GitHub buckets contributions in the user's own timezone.
    stats = calculate_streaks(weeks)

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

    # Monochrome: the achievement badges are the only colour on the page,
    # so the freshness dot is a shade of grey instead of green/amber/red.
    return f"""
<g transform="translate({CARD_WIDTH - 14},26)">
    <circle cx="-6" cy="-4" r="3" fill="{color}"/>
    <text
        x="-14"
        y="0"
        text-anchor="end"
        font-family="Segoe UI, Ubuntu, Sans-Serif"
        font-size="9.5"
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

    streak_start = data.get(
        "current_streak_start"
    )

    reference_date_value = data.get(
        "reference_date"
    ) or reference_date().isoformat()

    reference_year = int(
        reference_date_value[:4]
    )

    badge = freshness_badge(
        result
    )

    svg = f"""<svg
xmlns="http://www.w3.org/2000/svg"
width="{CARD_WIDTH}"
height="{CARD_HEIGHT}"
viewBox="0 0 {CARD_WIDTH} {CARD_HEIGHT}">

<style>
.title {{
    font: bold 15px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.label {{
    font: 11px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #8b949e;
}}

.value {{
    font: bold 44px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.small-value {{
    font: bold 12px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.small-label {{
    font: 9.5px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #8b949e;
}}
</style>

<rect
    x="0.5"
    y="0.5"
    width="{CARD_WIDTH - 1}"
    height="{CARD_HEIGHT - 1}"
    rx="7"
    fill="#0d1117"
    stroke="#30363d"
    stroke-width="1"
/>

{badge}

<g transform="translate(16,12)">
    <svg
        viewBox="0 0 16 16"
        width="17"
        height="17"
    >
        <path
            fill="#e6edf3"
            fill-rule="evenodd"
            d="M8.618.067a.75.75 0 00-.736.035C6.096 1.34 4 3.758 4 6.5c0 2.373 1.272 4.316 2.977 5.253C6.398 12.016 6 12.656 6 13.5c0 1.38 1.12 2.5 2.5 2.5s2.5-1.12 2.5-2.5c0-.844-.398-1.484-.977-1.747 1.705-.937 2.977-2.88 2.977-5.253 0-2.742-2.096-5.16-3.882-6.398a.75.75 0 00-.502-.135zM8.5 14.5c-.552 0-1-.448-1-1s.448-1 1-1 1 .448 1 1-.448 1-1 1"
        />
    </svg>
    <text x="24" y="13" class="title">
        Current Streak
    </text>
</g>

<text
    x="150"
    y="86"
    text-anchor="middle"
    class="value"
>
    {current}
</text>

<text
    x="150"
    y="106"
    text-anchor="middle"
    class="label"
>
    consecutive days
</text>

<line
    x1="16"
    y1="122"
    x2="{CARD_WIDTH - 16}"
    y2="122"
    stroke="#27272a"
    stroke-width="1"
/>

<text x="16" y="144" class="small-label">
    Longest (12 mo)
</text>

<text x="16" y="160" class="small-value">
    {longest} days
</text>

<text x="110" y="144" class="small-label">
    Contributions
</text>

<text x="110" y="160" class="small-value">
    {total}
</text>

<text x="204" y="144" class="small-label">
    Active Days
</text>

<text x="204" y="160" class="small-value">
    {active_days}
</text>

<text x="16" y="182" class="small-label">
    {reference_year} contributions: {current_year}
</text>

<text x="16" y="196" class="small-label">
    {("Since " + streak_start) if streak_start else "No active streak"}
</text>

<text x="{CARD_WIDTH - 16}" y="182" text-anchor="end" class="small-label">
    Last: {last_date or "none"}
</text>

<text x="{CARD_WIDTH - 16}" y="196" text-anchor="end" class="small-label">
    As of: {reference_date_value}
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

def contribution_count_on(
    contribution_data: dict[str, Any],
    day: Optional[str],
) -> Optional[int]:

    """
    Number of contributions recorded on a given ISO date.

    Used to show how much was contributed on the most recent active day.
    """

    if not day:
        return None

    weeks = contribution_data.get("weeks")

    if not isinstance(weeks, list):
        return None

    for week in weeks:

        if not isinstance(week, dict):
            continue

        days = week.get("contributionDays")

        if not isinstance(days, list):
            continue

        for day_data in days:

            if not isinstance(day_data, dict):
                continue

            if day_data.get("date") == day:
                return int(
                    day_data.get(
                        "contributionCount",
                        0,
                    )
                )

    return None


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

    total = contribution_data.get(
        "github_total_contributions",
        0,
    )

    current = int(
        contribution_data.get(
            "current_streak",
            0,
        )
    )

    longest = int(
        contribution_data.get(
            "longest_streak",
            0,
        )
    )

    active_days = int(
        contribution_data.get(
            "active_days",
            0,
        )
    )

    reference_date_value = contribution_data.get(
        "reference_date"
    ) or reference_date().isoformat()

    last_date = contribution_data.get(
        "last_contribution_date"
    )

    streak_start = contribution_data.get(
        "current_streak_start"
    )

    last_count = contribution_count_on(
        contribution_data,
        last_date,
    )

    if last_count is None:
        latest_value = last_date or "none"

    else:
        latest_value = f"{last_count} on {last_date}"

    since_value = streak_start or "no active streak"

    badge = freshness_badge(
        meta
    )

    # Two columns, same rows, so the card reads as a table.
    left_column = [
        ("Commits", str(commits)),
        ("Pull Requests", str(prs)),
        ("Active Days", str(active_days)),
        ("Longest Streak", f"{longest} days"),
    ]

    right_column = [
        ("Total Contributions", str(total)),
        ("Current Streak", f"{current} days"),
        ("Latest Contribution", latest_value),
        ("Since", since_value),
    ]

    def render_column(
        entries: list[tuple[str, str]],
        x: int,
    ) -> str:
        blocks = []

        for index, (label, value) in enumerate(entries):
            label_y = 64 + index * 34
            value_y = label_y + 15

            blocks.append(
                f'<text x="{x}" y="{label_y}" class="stat-label">'
                f"{label}</text>\n"
                f'<text x="{x}" y="{value_y}" class="stat-value">'
                f"{value}</text>"
            )

        return "\n".join(blocks)

    svg = f"""<svg
xmlns="http://www.w3.org/2000/svg"
width="{CARD_WIDTH}"
height="{CARD_HEIGHT}"
viewBox="0 0 {CARD_WIDTH} {CARD_HEIGHT}">

<style>
.title {{
    font: bold 15px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.stat-label {{
    font: 10.5px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #8b949e;
}}

.stat-value {{
    font: bold 12.5px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}
</style>

<rect
    x="0.5"
    y="0.5"
    width="{CARD_WIDTH - 1}"
    height="{CARD_HEIGHT - 1}"
    rx="7"
    fill="#0d1117"
    stroke="#30363d"
    stroke-width="1"
/>

{badge}

<g transform="translate(16,12)">
    <svg
        viewBox="0 0 16 16"
        width="17"
        height="17"
    >
        <path
            fill="#e6edf3"
            fill-rule="evenodd"
            d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"
        />
    </svg>
    <text x="24" y="13" class="title">
        GitHub Stats
    </text>
</g>

<line
    x1="16"
    y1="44"
    x2="{CARD_WIDTH - 16}"
    y2="44"
    stroke="#27272a"
    stroke-width="1"
/>

{render_column(left_column, 16)}

{render_column(right_column, 154)}

</svg>
"""

    output.write_text(
        svg,
        encoding="utf-8",
    )


# ============================================================================
# LANGUAGES SVG
# ============================================================================

# Grayscale ramp for the language bar. The profile is monochrome, so the
# languages are not painted in their own brand colors.
#
# Light and dark alternate, so neighbouring segments (and the matching
# legend dots) are always easy to tell apart.
LANGUAGE_GRAYSCALE = [
    "#e6edf3",
    "#484f58",
    "#c9d1d9",
    "#6e7681",
    "#adbac7",
    "#8b949e",
]

MAX_LEGEND_ENTRIES = 6


def fetch_repo_languages(
    deadline: float,
) -> tuple[Optional[list[dict[str, Any]]], FetchResult]:

    """
    Fetch the primary language of every public repository.

    This is a SECONDARY request: the streak data must never wait for it.
    On failure the caller keeps the previously rendered card.
    """

    result = graphql_request(
        STATS_GRAPHQL_QUERY,
        deadline,
    )

    if result.status != ResultStatus.SUCCESS or not result.data:
        return None, result

    user = result.data.get("user")

    if not isinstance(user, dict):
        return None, result

    repositories = user.get("repositories")

    if not isinstance(repositories, dict):
        return None, result

    nodes = repositories.get("nodes")

    if not isinstance(nodes, list):
        return None, result

    counts: dict[str, int] = {}

    for repo in nodes:

        if not isinstance(repo, dict):
            continue

        language = repo.get("primaryLanguage")

        name = None

        if isinstance(language, dict):
            name = language.get("name")

        if not isinstance(name, str) or not name:
            name = "Other"

        counts[name] = counts.get(name, 0) + 1

    total = sum(counts.values())

    if not total:
        return None, result

    languages = [
        {
            "name": name,
            "count": count,
            "percent": round(count / total * 100, 1),
        }
        for name, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]

    return languages, result


def make_languages_svg(
    languages: list[dict[str, Any]],
    meta: FetchResult,
) -> None:

    output = ASSETS_DIR / "languages.svg"

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    total_repos = sum(
        int(language.get("count", 0))
        for language in languages
    )

    shown = languages[:MAX_LEGEND_ENTRIES]

    hidden = len(languages) - len(shown)

    badge = freshness_badge(
        meta
    )

    # ------------------------------------------------------------------
    # PROGRESS BAR
    # ------------------------------------------------------------------

    bar_x = 16
    bar_width = CARD_WIDTH - 32

    segments: list[str] = []

    offset = 0.0

    for index, language in enumerate(shown):

        percent = float(
            language.get("percent", 0)
        )

        width = bar_width * percent / 100

        color = LANGUAGE_GRAYSCALE[
            index % len(LANGUAGE_GRAYSCALE)
        ]

        segments.append(
            f'<rect x="{bar_x + offset:.2f}" y="54" '
            f'width="{width:.2f}" height="8" fill="{color}" />'
        )

        offset += width

    # ------------------------------------------------------------------
    # LEGEND
    # ------------------------------------------------------------------

    legend: list[str] = []

    for index, language in enumerate(shown):

        column = index % 2
        row = index // 2

        x = 16 + column * 140
        y = 80 + row * 22

        color = LANGUAGE_GRAYSCALE[
            index % len(LANGUAGE_GRAYSCALE)
        ]

        name = str(
            language.get("name", "Other")
        )

        percent = float(
            language.get("percent", 0)
        )

        legend.append(
            f'<g transform="translate({x}, {y})">\n'
            f'    <circle cx="4" cy="4" r="4" fill="{color}" />\n'
            f'    <text x="13" y="8" class="lang-name">{name}</text>\n'
            f'    <text x="128" y="8" text-anchor="end" '
            f'class="lang-pct">{percent:.1f}%</text>\n'
            f'</g>'
        )

    hidden_note = (
        f"+{hidden} more languages"
        if hidden > 0
        else ""
    )

    svg = f"""<svg
xmlns="http://www.w3.org/2000/svg"
width="{CARD_WIDTH}"
height="{CARD_HEIGHT}"
viewBox="0 0 {CARD_WIDTH} {CARD_HEIGHT}">

<style>
.title {{
    font: bold 15px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.lang-name {{
    font: bold 10.5px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.lang-pct {{
    font: 10px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #8b949e;
}}

.small-label {{
    font: 9.5px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #8b949e;
}}
</style>

<rect
    x="0.5"
    y="0.5"
    width="{CARD_WIDTH - 1}"
    height="{CARD_HEIGHT - 1}"
    rx="7"
    fill="#0d1117"
    stroke="#30363d"
    stroke-width="1"
/>

{badge}

<g transform="translate(16,12)">
    <svg
        viewBox="0 0 16 16"
        width="17"
        height="17"
    >
        <path
            fill="#e6edf3"
            fill-rule="evenodd"
            d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"
        />
    </svg>
    <text x="24" y="13" class="title">
        Top Languages
    </text>
</g>

<clipPath id="language-bar-clip">
    <rect x="{bar_x}" y="54" width="{bar_width}" height="8" rx="4" />
</clipPath>

<g clip-path="url(#language-bar-clip)">
{chr(10).join(segments)}
</g>

{chr(10).join(legend)}

<line
    x1="16"
    y1="138"
    x2="{CARD_WIDTH - 16}"
    y2="138"
    stroke="#27272a"
    stroke-width="1"
/>

<text x="16" y="160" class="small-label">
    {total_repos} repositories · {len(languages)} languages
</text>

<text x="16" y="176" class="small-label">
    Share of repositories
</text>

<text x="{CARD_WIDTH - 16}" y="160" text-anchor="end" class="small-label">
    As of: {reference_date().isoformat()}
</text>

<text x="{CARD_WIDTH - 16}" y="176" text-anchor="end" class="small-label">
    {hidden_note if hidden_note else "all languages shown"}
</text>

</svg>
"""

    output.write_text(
        svg,
        encoding="utf-8",
    )

    log.info(
        "Languages SVG written: %d repos, %d languages",
        total_repos,
        len(languages),
    )


# ============================================================================
# LANGUAGES DONUT (summary row)
# ============================================================================

# The summary row used to show two near-identical donuts ("by repo" and
# "by commit"). There is now a single donut with an explicit legend.
#
# Shading alternates light/dark so neighbouring slices are unambiguous.
DONUT_WIDTH = 340
DONUT_HEIGHT = 200

DONUT_SHADES = [
    "#e6edf3",
    "#6e7681",
    "#adbac7",
    "#484f58",
    "#c9d1d9",
    "#8b949e",
    "#f0f6fc",
    "#3d444d",
]

DONUT_LEGEND_ENTRIES = 6


def make_languages_donut_svg(
    languages: list[dict[str, Any]],
) -> None:

    output = ASSETS_DIR / "languages-donut.svg"

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    ordered = sorted(
        languages,
        key=lambda item: (
            -float(item.get("percent", 0)),
            str(item.get("name", "")),
        ),
    )

    total_repos = sum(
        int(language.get("count", 0))
        for language in ordered
    )

    radius = 46.0
    circumference = 2 * math.pi * radius
    gap = 1.5

    slices: list[str] = []
    offset = 0.0

    for index, language in enumerate(ordered):

        percent = float(
            language.get("percent", 0)
        )

        length = max(
            0.0,
            circumference * percent / 100 - gap,
        )

        color = DONUT_SHADES[
            index % len(DONUT_SHADES)
        ]

        slices.append(
            f'<circle cx="88" cy="108" r="{radius}" '
            f'fill="none" stroke="{color}" stroke-width="18" '
            f'stroke-dasharray="{length:.2f} {circumference:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" />'
        )

        offset += circumference * percent / 100

    shown = ordered[:DONUT_LEGEND_ENTRIES]
    hidden = len(ordered) - len(shown)

    legend: list[str] = []

    for index, language in enumerate(shown):

        y = 46 + index * 21

        color = DONUT_SHADES[
            index % len(DONUT_SHADES)
        ]

        name = str(
            language.get("name", "Other")
        )

        percent = float(
            language.get("percent", 0)
        )

        legend.append(
            f'<g transform="translate(166, {y})">\n'
            f'    <rect x="0" y="-7" width="8" height="8" rx="1.5" '
            f'fill="{color}" />\n'
            f'    <text x="14" y="1" class="donut-name">{name}</text>\n'
            f'    <text x="158" y="1" text-anchor="end" '
            f'class="donut-pct">{percent:.1f}%</text>\n'
            f'</g>'
        )

    hidden_note = (
        f" · +{hidden} more"
        if hidden > 0
        else ""
    )

    svg = f"""<svg
xmlns="http://www.w3.org/2000/svg"
width="{DONUT_WIDTH}"
height="{DONUT_HEIGHT}"
viewBox="0 0 {DONUT_WIDTH} {DONUT_HEIGHT}">

<style>
.title {{
    font: bold 14px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.donut-name {{
    font: bold 10.5px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.donut-pct {{
    font: 10px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #8b949e;
}}

.center-value {{
    font: bold 18px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #e6edf3;
}}

.center-label {{
    font: 9px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #8b949e;
}}

.footer {{
    font: 9.5px 'Segoe UI', Ubuntu, Sans-Serif;
    fill: #8b949e;
}}
</style>

<rect
    x="0.5"
    y="0.5"
    width="{DONUT_WIDTH - 1}"
    height="{DONUT_HEIGHT - 1}"
    rx="7"
    fill="#0d1117"
    stroke="#30363d"
    stroke-width="1"
/>

<text x="16" y="26" class="title">
    Languages by Repository
</text>

<g transform="rotate(-90 88 108)">
{chr(10).join(slices)}
</g>

<text x="88" y="105" text-anchor="middle" class="center-value">
    {total_repos}
</text>

<text x="88" y="119" text-anchor="middle" class="center-label">
    repos
</text>

{chr(10).join(legend)}

<text x="16" y="182" class="footer">
    {len(ordered)} languages{hidden_note}
</text>

<text x="{DONUT_WIDTH - 16}" y="182" text-anchor="end" class="footer">
    as of {reference_date().isoformat()}
</text>

</svg>
"""

    output.write_text(
        svg,
        encoding="utf-8",
    )

    log.info(
        "Languages donut written: %d languages",
        len(ordered),
    )


# ============================================================================
# DATA JSON
# ============================================================================

def make_data_json(
    contribution_data: dict[str, Any],
    languages: Optional[list[dict[str, Any]]],
) -> None:

    """
    Write the machine readable snapshot used by the HTML card renderer.

    The SVG cards cannot be used on a page that runs JavaScript, so the
    same numbers are also published as JSON. The page falls back to its
    embedded snapshot when the file cannot be fetched (file:// pages are
    blocked by CORS).
    """

    output = ASSETS_DIR / "data.json"

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    previous: dict[str, Any] = {}

    if output.exists():
        try:
            previous = json.loads(
                output.read_text(
                    encoding="utf-8"
                )
            )

        except Exception:
            previous = {}

    if not languages:
        languages = previous.get("languages") or []

    repos = sum(
        int(language.get("count", 0))
        for language in languages
    )

    if not repos:
        repos = int(previous.get("repos", 0))

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

    payload = {
        "asOf": contribution_data.get(
            "reference_date"
        ) or reference_date().isoformat(),

        "since": contribution_data.get(
            "current_streak_start"
        ),

        "totalContributions": int(
            contribution_data.get(
                "github_total_contributions",
                contribution_data.get(
                    "total_contributions",
                    0,
                ),
            )
        ),

        "commits": int(commits),

        "pullRequests": int(
            contribution_data.get(
                "total_pull_request_contributions",
                0,
            )
        ),

        "activeDays": int(
            contribution_data.get(
                "active_days",
                0,
            )
        ),

        "currentStreak": int(
            contribution_data.get(
                "current_streak",
                0,
            )
        ),

        "longestStreak": int(
            contribution_data.get(
                "longest_streak",
                0,
            )
        ),

        "repos": repos,

        "languages": [
            {
                "name": str(language.get("name", "Other")),
                "pct": float(language.get("percent", 0)),
                "count": int(language.get("count", 0)),
            }
            for language in languages
        ],

        "updatedAt": now_iso(),
    }

    output.write_text(
        json.dumps(
            payload,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    log.info(
        "data.json written: streak=%s repos=%s languages=%d",
        payload["currentStreak"],
        payload["repos"],
        len(payload["languages"]),
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

    # ------------------------------------------------------------------
    # Timezone aware reference day
    # ------------------------------------------------------------------

    # User is AHEAD of UTC: the calendar ends one day after the UTC
    # date. The streak must still be counted from the user's own
    # current day, and the payload must not be rejected as "future".
    ahead = base + timedelta(days=1)

    weeks = _make_test_weeks(
        {
            ahead.isoformat(): 0,
            base.isoformat(): 1,
            (base - timedelta(days=1)).isoformat(): 1,
            (base - timedelta(days=2)).isoformat(): 1,
        }
    )

    valid, _, error = validate_calendar(
        weeks,
        utc_today=base,
    )

    assert valid, error

    result = calculate_streaks(
        weeks,
        utc_today=base,
    )

    # The anchor is "today" in the user's timezone (ahead), which has no
    # contributions yet, so the streak continues from the UTC day.
    assert result["reference_date"] == ahead.isoformat()
    assert result["current_streak"] == 3

    # User is BEHIND of UTC: the calendar ends one day before the UTC
    # date. The streak must be anchored on the user's current day.
    behind = base - timedelta(days=1)

    weeks = _make_test_weeks(
        {
            behind.isoformat(): 1,
            (behind - timedelta(days=1)).isoformat(): 1,
            (behind - timedelta(days=2)).isoformat(): 0,
        }
    )

    result = calculate_streaks(
        weeks,
        utc_today=base,
    )

    assert result["reference_date"] == behind.isoformat()
    assert result["current_streak"] == 2

    # A calendar that is clearly stale cannot prove the streak is alive.
    stale = base - timedelta(days=5)

    weeks = _make_test_weeks(
        {
            stale.isoformat(): 1,
            (stale - timedelta(days=1)).isoformat(): 1,
            (stale - timedelta(days=2)).isoformat(): 1,
        }
    )

    result = calculate_streaks(
        weeks,
        utc_today=base,
    )

    assert result["reference_date"] == base.isoformat()
    assert result["current_streak"] == 0

    # A day further in the future than the timezone tolerance is still
    # rejected outright.
    weeks = _make_test_weeks(
        {
            (base + timedelta(days=2)).isoformat(): 1,
        }
    )

    valid, _, error = validate_calendar(
        weeks,
        utc_today=base,
    )

    assert not valid
    assert error is not None

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

    # ---------------------------------------------------------------------
    # LANGUAGES CARD
    # ---------------------------------------------------------------------
    #
    # Secondary request: it must never block or fail the streak cards.
    # When it fails, the previously rendered card is kept untouched.

    languages, languages_meta = fetch_repo_languages(
        time.monotonic() + SECONDARY_BUDGET_SECONDS
    )

    if languages:

        make_languages_svg(
            languages,
            languages_meta,
        )

        make_languages_donut_svg(
            languages,
        )

    else:

        log.warning(
            "Language data unavailable - keeping the existing cards"
        )

    make_data_json(
        result.data,
        languages,
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
