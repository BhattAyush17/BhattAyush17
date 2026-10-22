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
    assets/stats/github-cards.svg    (3-across row used by the README)
    assets/stats/commit-hours.svg    (commits by hour, full width)
    assets/stats/github-stats.svg    (standalone card)
    assets/stats/languages.svg       (standalone card)
    assets/stats/streak.svg          (standalone card)
    assets/stats/data.json
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

import cards_svg


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

# ---------------------------------------------------------------------------
# AUTOMATION SELF-CONTRIBUTIONS
# ---------------------------------------------------------------------------
#
# This profile repository is updated by the automation itself, and every
# commit the bot makes lands on the default branch.
#
# GitHub does not count those commits in the contribution calendar: the
# publish step rebases the bot's commits onto the newest main, which
# rewrites their SHA, and a rewritten commit loses its contribution
# credit. Measured over the whole history of this repository, 23
# automation commits on a day contributed 0 to the calendar while the 9
# human commits on that day contributed exactly 9.
#
# The calendar is therefore already free of automation noise, and the
# numbers must be reported exactly as GitHub reports them. Subtracting
# the bot's commits would be wrong: it would wipe out days that have
# real human work on them.
#
# The one thing that still has to be handled is the feedback loop. A bot
# commit must never cause the next run to publish a new commit, or the
# automation would keep itself busy forever. That is prevented by
# comparing the profile repository's history against the one seen during
# the previous run: when only the bot committed since then, the previous
# numbers are kept, because they cannot have changed.

BOT_AUTHOR = "github-actions[bot]"

# The head of the profile repository, used to detect whether only the
# automation worked since the previous run.
PROFILE_HISTORY_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 50) {
            nodes {
              oid
              author {
                name
              }
            }
          }
        }
      }
    }
  }
}
"""


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
# PROFILE HISTORY
# ============================================================================
#
# The head of the profile repository, used to detect whether only the
# automation worked since the previous run.

def fetch_profile_history(
    deadline: float,
) -> tuple[Optional[str], list[dict[str, Any]]]:
    """
    The head commit of the profile repository and its recent history.

    Returns the head SHA and a list of commits, newest first, each with
    a `bot` flag telling whether the automation made it.

    The head SHA is what makes the feedback loop detectable: the next
    run compares it with the one seen last time and can tell whether
    anything but the bot worked in the meantime.
    """

    if not TOKEN:
        return None, []

    try:

        response = SESSION.post(
            GRAPHQL_URL,
            json={
                "query": PROFILE_HISTORY_QUERY,
                "variables": {
                    "owner": USERNAME,
                    "name": USERNAME,
                },
            },
            timeout=min(
                MAX_REQUEST_TIMEOUT,
                max(0.5, deadline - time.monotonic() - 0.1),
            ),
        )

        response.raise_for_status()

        payload = response.json()

    except Exception as exc:

        log.warning(
            "Profile history lookup failed: %s",
            exc,
        )
        return None, []

    if payload.get("errors"):

        log.warning(
            "Profile history lookup returned errors: %s",
            payload["errors"],
        )
        return None, []

    data = payload.get("data") or {}

    repository = data.get("repository") or {}

    default_branch = repository.get("defaultBranchRef") or {}

    target = default_branch.get("target") or {}

    history = target.get("history") or {}

    commits: list[dict[str, Any]] = []

    for node in history.get("nodes") or []:

        author = node.get("author") or {}

        commits.append(
            {
                "sha": node.get("oid") or "",
                "bot": author.get("name") == BOT_AUTHOR,
            }
        )

    head = commits[0]["sha"] if commits else None

    log.info(
        "Profile repository head: %s (%d recent commits)",
        head[:8] if head else "unknown",
        len(commits),
    )

    return head, commits


def only_automation_worked(
    previous_head: Optional[str],
    commits: list[dict[str, Any]],
) -> bool:
    """
    True when every commit made since the previous run was the bot's.

    In that case the contribution numbers cannot have changed, so the
    previous numbers are kept and nothing is published. Without this the
    automation would keep reacting to its own commits.
    """

    if not previous_head or not commits:
        return False

    for commit in commits:

        if commit.get("sha") == previous_head:
            # Everything before this commit is newer than the previous
            # run. The first entry is the head itself, so it is skipped.
            newer = commits[: commits.index(commit)]

            if not newer:
                return False

            return all(entry.get("bot") for entry in newer)

    # The previous head is no longer in the history (a force push or a
    # rebase). The situation is unknown, so the fresh data is used.
    return False


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

    # Keep the previous timestamp when the cached DATA is identical.
    #
    # The sync workflow runs every few minutes. A fresh `cached_at` on
    # every run would change the cache file every time, which means a
    # commit every few minutes that changes nothing. The timestamp
    # therefore only moves when the cached data actually moves.

    try:

        if CACHE_FILE.exists():

            previous_payload = json.loads(
                CACHE_FILE.read_text(
                    encoding="utf-8"
                )
            )

            if (
                previous_payload.get("data")
                == payload["data"]
                and previous_payload.get("cached_at")
            ):

                payload["cached_at"] = previous_payload["cached_at"]

                log.info(
                    "Cache data unchanged - keeping cached_at %s",
                    payload["cached_at"],
                )

    except Exception:

        log.info(
            "Cache comparison failed - using a fresh timestamp",
        )

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

    # The head of the profile repository tells us whether only the
    # automation worked since the previous run.
    head_sha, commits = fetch_profile_history(deadline)

    previous_head = None

    if cache and cache.data:
        previous_head = cache.data.get(
            "_profile_head_sha"
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

            only_bot = only_automation_worked(
                previous_head,
                commits,
            )

            if only_bot and cache and cache.data:

                fresh_total = int(
                    normalized.get(
                        "github_total_contributions",
                        0,
                    )
                    or 0
                )

                cached_total = int(
                    cache.data.get(
                        "github_total_contributions",
                        0,
                    )
                    or 0
                )

                if fresh_total > cached_total:

                    # The graph caught up with real new work. The
                    # numbers are used after all, otherwise a commit
                    # could be missed forever.
                    log.info(
                        "Contribution count grew from %d to %d - "
                        "using the fresh numbers",
                        cached_total,
                        fresh_total,
                    )

                    only_bot = False

            if only_bot and cache and cache.data:

                # Only the bot committed and no new work showed up, so
                # the contribution numbers cannot have changed. The
                # previous numbers are kept and nothing is published.
                # Without this the automation would react to its own
                # commits forever.
                #
                # The remembered head is deliberately NOT advanced here.
                # It marks the last time the numbers really changed, and
                # advancing it would change the cache file on every bot
                # commit, which would publish a commit, which would make
                # the bot commit again.
                log.info(
                    "Only the automation worked - keeping the "
                    "previous contribution numbers"
                )

                normalized = dict(cache.data)

            elif head_sha:

                normalized["_profile_head_sha"] = head_sha

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
# LANGUAGES DATA
# ============================================================================

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


# ============================================================================
# MONOCHROME CARDS (3 across)
# ============================================================================

COMBINED_CARDS_FILE = ASSETS_DIR / "github-cards.svg"


def _cards_languages(
    languages: Optional[list[dict[str, Any]]],
) -> list[dict[str, Any]]:

    """Normalize fresh language data, or fall back to the last data.json."""

    if languages:
        return [
            {
                "name": item["name"],
                "pct": float(item.get("percent", 0)),
                "count": int(item.get("count", 0)),
            }
            for item in languages
        ]

    try:
        previous = json.loads(
            (ASSETS_DIR / "data.json").read_text(encoding="utf-8")
        )
        return list(previous.get("languages") or [])

    except (OSError, ValueError):
        return []


HOURLY_WINDOW_DAYS = 365

# Set when the hourly distribution was rewritten, so the freshness
# footer can follow it even though it is not a readme_sync surface.
HOURLY_CHANGED = False

IST = timezone(timedelta(hours=5, minutes=30))


def fetch_hourly_counts(
    deadline: float,
) -> Optional[list[int]]:
    """
    Commits per hour of the day for the last year, in IST.

    The GraphQL contribution calendar only returns daily totals, so the
    commit search API is the one source that carries real commit times.
    It paginates 100 at a time and stops indexing at 1000 results, which
    is plenty for one year of personal commits.

    Returns None when the lookup fails, so the caller can keep the
    previously committed snapshot instead of blanking the chart.
    """

    if not TOKEN:
        return None

    since = (
        datetime.now(timezone.utc) - timedelta(days=HOURLY_WINDOW_DAYS)
    ).strftime("%Y-%m-%d")

    query = f"author:{USERNAME} author-date:>={since}"

    counts = [0] * 24
    seen_shas: set[str] = set()
    page = 1

    while page <= 10:

        if time.monotonic() >= deadline:
            log.warning(
                "Commit hour lookup ran out of time on page %d", page
            )
            return None

        try:

            response = SESSION.get(
                "https://api.github.com/search/commits",
                params={
                    "q": query,
                    "sort": "author-date",
                    "order": "desc",
                    "per_page": 100,
                    "page": page,
                },
                timeout=min(
                    MAX_REQUEST_TIMEOUT,
                    max(0.5, deadline - time.monotonic()),
                ),
            )

            response.raise_for_status()

            payload = response.json()

        except Exception as exc:

            log.warning(
                "Commit hour lookup failed on page %d: %s", page, exc
            )
            return None

        items = payload.get("items") or []

        if not items:
            break

        for item in items:

            # Paginated search results are not guaranteed to be stable:
            # a commit can drift onto two adjacent pages while the index
            # settles. Counting by SHA keeps the total exact.
            sha = item.get("sha")

            if sha:
                if sha in seen_shas:
                    continue
                seen_shas.add(sha)

            raw = ((item.get("commit") or {}).get("author") or {}).get("date")

            if not raw:
                continue

            try:
                stamp = datetime.fromisoformat(
                    raw.replace("Z", "+00:00")
                )
            except ValueError:
                continue

            counts[stamp.astimezone(IST).hour] += 1

        seen = len(seen_shas)

        total = int(payload.get("total_count") or 0)

        if len(items) < 100 or seen >= min(total, 1000):
            break

        page += 1

    if seen == 0:
        log.warning("Commit hour lookup returned no commits")
        return None

    log.info(
        "Commit hours: %d commits across %d hour buckets",
        seen,
        sum(1 for value in counts if value),
    )

    return counts


def write_hourly_json(counts: list[int]) -> None:
    """
    Persist the hourly counts, keeping the old file byte-identical when
    the distribution has not moved. Otherwise every run would produce a
    commit that changes nothing but a timestamp.
    """

    path = ASSETS_DIR / "hourly.json"

    global HOURLY_CHANGED

    previous: Optional[dict[str, Any]] = None

    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None

    if previous and previous.get("counts") == counts:
        return

    payload = {
        "tz": "UTC+5:30 (IST)",
        "counts": counts,
        "total": sum(counts),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }

    path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )

    HOURLY_CHANGED = True

    log.info(
        "hourly.json updated: %d commits", sum(counts)
    )


def make_cards_svg(
    result: FetchResult,
    languages: Optional[list[dict[str, Any]]],
) -> None:

    """
    Write the three monochrome cards plus ONE combined 3-across SVG.

    The README embeds github-cards.svg with width=100%, so the row can
    never wrap to 2 + 1 the way three separate 290px images did.
    """

    data = result.data or {}

    langs = _cards_languages(languages)

    total = int(
        data.get(
            "github_total_contributions",
            data.get("total_contributions", 0),
        )
    )

    commits = int(
        data.get("total_commit_contributions", 0)
        + data.get("restricted_contributions", 0)
    )

    card_data = {
        "total": total,
        "commits": commits,
        "prs": int(data.get("total_pull_request_contributions", 0)),
        "active_days": int(data.get("active_days", 0)),
        "current": int(data.get("current_streak", 0)),
        "longest": int(data.get("longest_streak", 0)),
        "since": data.get("current_streak_start") or "",
        "repos": sum(int(item.get("count", 0)) for item in langs),
        "as_of": data.get("reference_date")
        or reference_date().isoformat(),
        "languages": langs,
    }

    badge = result.badge()

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    STREAK_DIR.mkdir(parents=True, exist_ok=True)

    outputs = {
        ASSETS_DIR / "github-stats.svg": "stats",
        ASSETS_DIR / "streak.svg": "streak",
        STREAK_DIR / "streak.svg": "streak",
    }

    # Keep the previous languages card when the language fetch failed
    # and there is no earlier snapshot to draw from.
    if langs:
        outputs[ASSETS_DIR / "languages.svg"] = "languages"

    for path, kind in outputs.items():
        path.write_text(
            cards_svg.render_single(kind, card_data, badge),
            encoding="utf-8",
        )

    COMBINED_CARDS_FILE.write_text(
        cards_svg.render_combined(card_data, badge),
        encoding="utf-8",
    )

    # Commits-by-hour card. Real commit timestamps come from the commit
    # search API. When that lookup fails the last committed snapshot is
    # rendered unchanged, so a transient API error never blanks the
    # chart or silently lies about freshness.
    hourly = fetch_hourly_counts(time.monotonic() + 25)
    live_hours = hourly is not None

    if hourly is not None:

        try:
            write_hourly_json(hourly)
        except OSError as exc:
            log.warning("hourly.json not written: %s", exc)

    else:

        try:
            snapshot = json.loads(
                (ASSETS_DIR / "hourly.json").read_text(encoding="utf-8")
            )
            hourly = [int(value) for value in snapshot["counts"]]
            log.warning(
                "Commit hour lookup failed - rendering the snapshot"
            )
        except (OSError, ValueError, KeyError):
            hourly = None
            log.warning(
                "hourly data unavailable - commit-hours.svg not updated"
            )

    if hourly is not None:

        (ASSETS_DIR / "commit-hours.svg").write_text(
            cards_svg.render_hourly(
                hourly,
                "UTC+5:30 (IST)",
                badge if live_hours else ("#6e7681", "snapshot"),
            ),
            encoding="utf-8",
        )

    log.info("Cards written: %s", COMBINED_CARDS_FILE)


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

    # The snapshot timestamp must only move when the DATA moves.
    #
    # The sync workflow runs every few minutes. If updatedAt changed on
    # every run, data.json would differ on every run, the workflow would
    # commit every few minutes, and the contribution graph would fill
    # with commits that changed nothing.
    #
    # So the previous timestamp is kept whenever the payload is
    # otherwise identical, and the file only changes when the numbers
    # change.

    comparable = {
        key: value
        for key, value in payload.items()
        if key != "updatedAt"
    }

    previous_comparable = {
        key: value
        for key, value in previous.items()
        if key != "updatedAt"
    }

    if previous_comparable == comparable and previous.get("updatedAt"):

        payload["updatedAt"] = previous["updatedAt"]

        log.info(
            "data.json unchanged - keeping updatedAt %s",
            payload["updatedAt"],
        )

    else:

        log.info(
            "data.json changed - updatedAt %s",
            payload["updatedAt"],
        )

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

    # ---------------------------------------------------------------------
    # LANGUAGES CARD
    # ---------------------------------------------------------------------
    #
    # Secondary request: it must never block or fail the streak cards.
    # When it fails, the previously rendered card is kept untouched.

    languages, _ = fetch_repo_languages(
        time.monotonic() + SECONDARY_BUDGET_SECONDS
    )

    if not languages:

        log.warning(
            "Language data unavailable - keeping the existing cards"
        )

    make_cards_svg(
        result,
        languages,
    )

    make_data_json(
        result.data,
        languages,
    )

    # ---------------------------------------------------------------------
    # README SYNC
    # ---------------------------------------------------------------------
    #
    # Every GitHub derived block in README.md (recent repos, pinned
    # repos, achievements, the neofetch card and the freshness stamp) is
    # refreshed from the live API here, so the profile never shows a
    # number that GitHub already changed.
    #
    # readme_sync is fail-safe: a failed fetch leaves the previous
    # content in place, it never writes a half-updated README.

    try:
        import readme_sync

        # The hourly chart is not one of readme_sync's surfaces, so a
        # run that only moved that chart would leave the freshness
        # footer pointing at an older publish. Refresh it here too.
        if HOURLY_CHANGED:
            readme_sync.update_sync_stamp()

        readme_sync.sync_all()

    except Exception as exc:

        log.warning(
            "README sync skipped: %s",
            exc,
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
