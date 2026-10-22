#!/usr/bin/env python3
"""
readme_sync.py
---------------

Keeps every GitHub-derived element of the profile in sync with the live
GitHub state, so the README never shows a number that GitHub already
changed.

Managed surfaces
----------------
    README.md  recent_repos    $ gh repo list --sort pushed --limit 3
    README.md  pinned_repos    $ gh repo list --pinned --limit 4
    README.md  achievements    $ gh achievement list --user <user>
    README.md  sync_stamp      "last synced" freshness line
    README.md  neofetch_card   neofetch_card.svg (live GitHub numbers)
    index.html fallback        embedded data.json snapshot

Design rules
------------
    * Fail-safe. Every updater returns False and leaves the previous
      content untouched when the live fetch fails. The README is never
      left half-written or showing a placeholder.
    * Atomic. Files are written to a temp file and renamed, so a reader
      never sees a truncated file.
    * Idempotent. Running twice in a row produces byte-identical output
      when nothing changed upstream, so the workflow stays quiet.
    * No invented data. If GitHub does not report a value, the previous
      value is kept rather than guessed.

Environment:
    GH_USERNAME      GitHub username (default: BhattAyush17)
    GH_STATS_TOKEN   Token used for GraphQL and higher REST rate limits
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger("readme-sync")

USERNAME = os.getenv("GH_USERNAME", "BhattAyush17")
TOKEN = os.getenv("GH_STATS_TOKEN", "")

API_ROOT = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"
PROFILE_URL = f"https://github.com/{USERNAME}"

README_PATH = Path("README.md")
INDEX_PATH = Path("index.html")
NEOFETCH_PATH = Path("neofetch_card.svg")
BADGES_DIR = Path("assets/badges")
DATA_JSON_PATH = Path("assets/stats/data.json")

REQUEST_TIMEOUT = 15.0
MAX_RETRIES = 2
RETRY_BACKOFF = 1.5

# How many rows each README table shows.
RECENT_REPO_LIMIT = 3
PINNED_REPO_LIMIT = 4

# Shield colours, matched to the existing README palette.
SHIELD_BG = "1a1a1a"
SHIELD_COLOR = "6b7280"
SHIELD_LABEL_BG = "1a1a1a"

# One short phrase per achievement, used in the caption under the badges.
ACHIEVEMENT_NOTES = {
    "pull shark": "merged PRs",
    "yolo": "unreviewed merges",
    "quickdraw": "same-day issue closes",
    "galaxy brain": "multi-language PRs",
    "starstruck": "a repo got starred",
    "pair extraordinaire": "co-authored commits",
    "public sponsor": "sponsored a project",
    "open sourcerer": "contributed to open source",
    "arctic code vault contributor": "contributed to the Arctic Code Vault",
}


class SyncError(RuntimeError):
    """Raised when a live fetch cannot produce usable data."""


# ============================================================================
# HTTP
# ============================================================================

def _headers(accept: str = "application/vnd.github+json") -> dict[str, str]:

    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "readme-sync",
    }

    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    return headers


def _request(
    url: str,
    *,
    accept: str = "application/vnd.github+json",
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> requests.Response:

    last_error: Optional[Exception] = None

    for attempt in range(MAX_RETRIES + 1):

        try:
            response = requests.request(
                "POST" if json_body is not None else "GET",
                url,
                headers=_headers(accept),
                params=params,
                json=json_body,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in (403, 429) and attempt < MAX_RETRIES:
                # Rate limited: honour Retry-After when present, else back off.
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else RETRY_BACKOFF * (attempt + 1)
                )
                log.warning(
                    "Rate limited (%s) on %s - retrying in %.1fs",
                    response.status_code,
                    url,
                    delay,
                )
                time.sleep(delay)
                continue

            response.raise_for_status()

            return response

        except requests.RequestException as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            raise

    raise SyncError(f"request failed: {url} ({last_error})")


def _get_json(
    url: str,
    params: Optional[dict[str, Any]] = None,
) -> Any:
    response = _request(url, params=params)
    return response.json()


def _get_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    response = _request(
        GRAPHQL_URL,
        accept="application/json",
        json_body={"query": query, "variables": variables},
    )
    payload = response.json()

    if payload.get("errors"):
        raise SyncError(f"GraphQL errors: {payload['errors']}")

    return payload.get("data") or {}


def _get_text(url: str) -> str:
    response = _request(url, accept="text/html")
    return response.text


# ============================================================================
# FILE HELPERS
# ============================================================================

def _atomic_write(path: Path, text: str) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)

    handle, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )

    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _read_text(path: Path) -> Optional[str]:

    if not path.exists():
        return None

    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def replace_section(
    content: str,
    name: str,
    body: str,
) -> str:
    """
    Replace the block between the START/END markers of a managed section.

    The replacement is done with a function so that backslashes and
    group references inside the body are never interpreted.
    """

    start = f"<!-- START_SECTION:{name} -->"
    end = f"<!-- END_SECTION:{name} -->"

    if start not in content or end not in content:
        raise SyncError(
            f"markers for section '{name}' not found in README.md"
        )

    block = f"{start}\n{body}\n{end}"

    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end),
        re.DOTALL,
    )

    updated, count = pattern.subn(lambda _: block, content, count=1)

    if count != 1:
        raise SyncError(f"section '{name}' was not replaced")

    return updated


# ============================================================================
# MARKDOWN HELPERS
# ============================================================================

def _shield_label(text: str) -> str:
    """
    Escape a label for a shields.io static badge path segment.

    shields.io uses `_` for a space, so an underscore in the original
    text has to be doubled and a hyphen has to be doubled as well.
    """

    return (
        text.replace("_", "__")
        .replace("-", "--")
        .replace(" ", "_")
    )


def _repo_badge(name: str) -> str:
    return (
        f"https://img.shields.io/badge/{_shield_label(name)}-{SHIELD_BG}"
        f"?style=flat-square&logo=github&logoColor=white"
    )


def _count_badge(owner: str, repo: str, kind: str) -> str:
    return (
        f"https://img.shields.io/github/{kind}/{owner}/{repo}"
        f"?style=flat-square&color={SHIELD_COLOR}&labelColor={SHIELD_LABEL_BG}"
    )


def _truncate(text: str, limit: int = 50) -> str:

    text = (text or "").strip()

    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."

    return text


def _escape_cell(text: str) -> str:
    """Escape the characters that would break a markdown table cell."""
    return (
        (text or "")
        .replace("|", "\\|")
        .replace("\n", " ")
        .strip()
    )


def _format_date(value: Optional[str]) -> str:

    if not value:
        return "—"

    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return "—"

    return parsed.strftime("%b %d, %Y")


# ============================================================================
# SECTION: recent repos
# ============================================================================

def fetch_recent_repos(
    limit: int = RECENT_REPO_LIMIT,
) -> list[dict[str, Any]]:
    """
    Repositories of the user, most recently pushed first.

    The profile repository itself is skipped: it is the README, not a
    project, and it is always the most recently pushed repo.
    """

    payload = _get_json(
        f"{API_ROOT}/users/{USERNAME}/repos",
        params={"sort": "pushed", "per_page": 30},
    )

    if not isinstance(payload, list):
        raise SyncError("unexpected repo list payload")

    selected: list[dict[str, Any]] = []

    for repo in payload:
        if repo.get("name", "").lower() == USERNAME.lower():
            continue
        if repo.get("fork"):
            continue
        if repo.get("archived"):
            continue
        selected.append(repo)
        if len(selected) >= limit:
            break

    if not selected:
        raise SyncError("no recent repositories returned")

    return selected


def render_recent_repos(
    repos: list[dict[str, Any]],
) -> str:

    lines = [
        "| `repo` | `description` | `last active` |",
        "|--------|---------------|---------------|",
    ]

    for repo in repos:
        name = repo.get("name", "")
        url = repo.get("html_url") or f"{API_ROOT}/repos/{USERNAME}/{name}"
        description = _truncate(
            repo.get("description") or "No description"
        )
        pushed = _format_date(repo.get("pushed_at"))

        lines.append(
            f"| [![{name}]({_repo_badge(name)})]({url}) "
            f"| {_escape_cell(description)} | {pushed} |"
        )

    return "\n".join(lines)


def update_recent_repos() -> bool:

    try:
        repos = fetch_recent_repos()
        content = _read_text(README_PATH)

        if content is None:
            raise SyncError("README.md not found")

        updated = replace_section(
            content,
            "recent_repos",
            render_recent_repos(repos),
        )

        if updated == content:
            log.info("recent_repos already up to date")
            return False

        _atomic_write(README_PATH, updated)
        log.info("recent_repos updated: %s", ", ".join(
            r.get("name", "?") for r in repos
        ))
        return True

    except Exception as exc:
        log.warning("recent_repos not updated: %s", exc)
        return False


# ============================================================================
# SECTION: pinned repos
# ============================================================================

PINNED_QUERY = """
query($login: String!, $n: Int!) {
  user(login: $login) {
    pinnedItems(first: $n, types: REPOSITORY) {
      nodes {
        ... on Repository {
          name
          description
          stargazerCount
          forkCount
          primaryLanguage { name }
        }
      }
    }
  }
}
"""


def _dominant_language(name: str) -> Optional[str]:
    """
    The most used language of a repository.

    Used for repositories that GitHub reports without a primary
    language (for example a repository whose only file is a notebook).
    """

    try:
        payload = _get_json(
            f"{API_ROOT}/repos/{USERNAME}/{name}/languages"
        )
    except Exception:
        return None

    if not isinstance(payload, dict) or not payload:
        return None

    top = max(payload.items(), key=lambda item: item[1])[0]

    return str(top)


def fetch_pinned_repos(
    limit: int = PINNED_REPO_LIMIT,
) -> list[dict[str, Any]]:
    """Repositories pinned on the profile, in pin order."""

    data = _get_graphql(
        PINNED_QUERY,
        {"login": USERNAME, "n": limit},
    )

    user = data.get("user") or {}
    pinned = user.get("pinnedItems") or {}
    nodes = pinned.get("nodes") or []

    repos = [node for node in nodes if node and node.get("name")]

    if not repos:
        raise SyncError("no pinned repositories returned")

    return repos[:limit]


def render_pinned_repos(
    repos: list[dict[str, Any]],
) -> str:

    lines = [
        "| `repo` | `description` | `lang` | `⭐` | `🍴` |",
        "|--------|---------------|--------|------|------|",
    ]

    for repo in repos:
        name = repo.get("name", "")
        url = f"https://github.com/{USERNAME}/{name}"
        description = _truncate(
            repo.get("description") or "No description"
        )

        language = (repo.get("primaryLanguage") or {}).get("name")

        if not language:
            language = _dominant_language(name)

        language = language or "—"

        stars = _count_badge(USERNAME, name, "stars")
        forks = _count_badge(USERNAME, name, "forks")

        lines.append(
            f"| [![{name}]({_repo_badge(name)})]({url}) "
            f"| {_escape_cell(description)} | {language} "
            f"| ![]({stars}) | ![]({forks}) |"
        )

    return "\n".join(lines)


def update_pinned_repos() -> bool:

    try:
        repos = fetch_pinned_repos()
        content = _read_text(README_PATH)

        if content is None:
            raise SyncError("README.md not found")

        updated = replace_section(
            content,
            "pinned_repos",
            render_pinned_repos(repos),
        )

        if updated == content:
            log.info("pinned_repos already up to date")
            return False

        _atomic_write(README_PATH, updated)
        log.info("pinned_repos updated: %s", ", ".join(
            r.get("name", "?") for r in repos
        ))
        return True

    except Exception as exc:
        log.warning("pinned_repos not updated: %s", exc)
        return False


# ============================================================================
# SECTION: achievements
# ============================================================================

_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r'([a-zA-Z0-9_:.-]+)\s*=\s*"([^"]*)"')


def fetch_achievement_badges() -> list[tuple[str, str]]:
    """
    (alt, image url) for every achievement badge on the profile.

    GitHub renders the earned achievements in the profile sidebar with
    the class `achievement-badge-sidebar`. The markdown badges that the
    profile README itself shows are ignored on purpose, otherwise the
    script would just read back its own output.
    """

    html = _get_text(PROFILE_URL)

    badges: list[tuple[str, str]] = []
    seen: set[str] = set()

    for tag in _IMG_TAG.findall(html):

        attrs = dict(_ATTR.findall(tag))
        classes = attrs.get("class", "")

        if "achievement-badge-sidebar" not in classes:
            continue

        src = attrs.get("src", "").strip()
        alt = attrs.get("alt", "").replace("Achievement:", "").strip()

        if not src or not alt:
            continue

        if src in seen:
            continue

        seen.add(src)
        badges.append((alt, src))

    if not badges:
        raise SyncError("no achievement badges found on the profile")

    return badges


def _badge_filename(alt: str) -> str:
    slug = re.sub(
        r"[^a-zA-Z0-9_-]",
        "",
        alt.lower().replace(" ", "_"),
    )
    return f"achievement_{slug}.png"


def _file_version(path: Path) -> str:
    """
    Content hash used as the image cache buster.

    GitHub caches images by URL, so a changed badge needs a changed URL.
    A content hash changes exactly when the file changes, and a
    timestamp would change on every run even when the badge did not.
    """

    import hashlib

    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""

    return digest[:12]


def update_achievements() -> bool:

    try:
        badges = fetch_achievement_badges()
        content = _read_text(README_PATH)

        if content is None:
            raise SyncError("README.md not found")

        images: list[str] = []
        names: list[str] = []
        notes: list[str] = []

        for alt, url in badges:

            filename = _badge_filename(alt)
            target = BADGES_DIR / filename

            try:
                response = _request(url, accept="image/png")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(response.content)
            except Exception as exc:
                # Keep the badge that is already committed rather than
                # dropping it from the README.
                log.warning(
                    "badge download failed (%s): %s - keeping local copy",
                    alt,
                    exc,
                )
                if not target.exists():
                    continue

            version = _file_version(target)
            query = f"?v={version}" if version else ""

            images.append(
                f'<img src="assets/badges/{filename}{query}" width="72px" '
                f'alt="{alt}" title="{alt}" />'
            )
            names.append(alt)
            notes.append(
                ACHIEVEMENT_NOTES.get(
                    alt.lower(),
                    "a GitHub achievement",
                )
            )

        if not images:
            raise SyncError("no achievement images could be produced")

        body = " &nbsp;&nbsp; ".join(images)

        if names and notes:
            body += (
                "\n\n<sub>"
                + " · ".join(names)
                + " — "
                + ", ".join(notes)
                + ".</sub>"
            )

        updated = replace_section(
            content,
            "achievements",
            body,
        )

        if updated == content:
            log.info("achievements already up to date")
            return False

        _atomic_write(README_PATH, updated)
        log.info("achievements updated: %s", ", ".join(names))
        return True

    except Exception as exc:
        log.warning("achievements not updated: %s", exc)
        return False


# ============================================================================
# SECTION: sync stamp
# ============================================================================

STAMP_PLACEHOLDER = "0000-00-00 00:00:00 UTC"

STAMP_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC")


def render_sync_stamp(stamp: str) -> str:
    """
    Build the right-aligned freshness footer.

    Three parts, each doing a different job:

      * the exact UTC time the metadata was last regenerated. A static
        file can only carry a new time when something is committed, so
        this is written on every publish.
      * how often the pipeline re-checks GitHub, so an old timestamp is
        not mistaken for a stalled automation.
      * a live workflow badge. shields.io reads it straight from GitHub,
        so it turns green on every successful run and red the moment the
        sync stops working - without a single commit.

    Everything sits inside <p align="right">, which is the only
    alignment GitHub's markdown sanitizer keeps, so the block stays
    pinned to the right edge of the rendered page.
    """

    repo = os.getenv("GH_REPO", f"{USERNAME}/{USERNAME}")

    status_badge = (
        "https://img.shields.io/github/actions/workflow/status/"
        f"{repo}/sync-readme.yml"
        "?branch=main"
        "&style=flat-square"
        "&label=sync"
        "&labelColor=1a1a1a"
        "&color=6b7280"
        "&logo=github"
        "&logoColor=e5e7eb"
    )

    return (
        '<p align="right"><sub>'
        f"last synced <code>{stamp}</code>"
        " &nbsp;·&nbsp; auto-checks every 5 min"
        " &nbsp;·&nbsp; "
        f'<a href="https://github.com/{repo}/actions/workflows/sync-readme.yml">'
        f'<img src="{status_badge}" alt="sync status" height="18" />'
        "</a>"
        "</sub></p>"
    )


def section_body(content: str, name: str) -> Optional[str]:
    """
    Return the text between a section's markers, or None when absent.
    """

    start = f"<!-- START_SECTION:{name} -->"
    end = f"<!-- END_SECTION:{name} -->"

    if start not in content or end not in content:
        return None

    pattern = re.compile(
        re.escape(start) + r"(.*?)" + re.escape(end),
        re.DOTALL,
    )

    match = pattern.search(content)

    return match.group(1) if match else None


def sync_stamp_needs_refresh() -> bool:
    """
    True when the footer is missing or was written by an older template.

    Without this the footer could only change when some unrelated
    surface changed too, so a template fix would sit in the code for
    days before it reached the README.
    """

    content = _read_text(README_PATH)

    if content is None:
        return False

    current = section_body(content, "sync_stamp")

    if current is None:
        return True

    normalised = STAMP_TIMESTAMP.sub(STAMP_PLACEHOLDER, current)

    return normalised.strip() != render_sync_stamp(STAMP_PLACEHOLDER).strip()


def update_sync_stamp() -> bool:

    try:
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        content = _read_text(README_PATH)

        if content is None:
            raise SyncError("README.md not found")

        updated = replace_section(content, "sync_stamp", render_sync_stamp(stamp))

        if updated == content:
            return False

        _atomic_write(README_PATH, updated)
        log.info("sync_stamp updated: %s", stamp)
        return True

    except Exception as exc:
        log.warning("sync_stamp not updated: %s", exc)
        return False


# ============================================================================
# ASSET: neofetch card
# ============================================================================

# Fedora logo, lifted verbatim from the original hand-drawn card so the
# badge stays pixel identical after the card is regenerated.
#
# The path is split on whitespace only. Cutting it at an arbitrary
# offset would split a number such as "c6.626" into "c6.6 26" and
# silently corrupt the shape.
FEDORA_PATH = (
    "M12.001 0C5.376 0 0.008 5.369 0.004 11.992H0.002v9.287h0.002A2.726 2.726 0 0 0 2.73 "
    "24h9.275c6.626 -0.004 11.993 -5.372 11.993 -11.997C23.998 5.375 18.628 0 12 0zm2.431 "
    "4.94c2.015 0 3.917 1.543 3.917 3.671 0 0.197 0.001 0.395 -0.03 0.619a1.002 1.002 0 0 1 "
    "-1.137 0.893 1.002 1.002 0 0 1 -0.842 -1.175 2.61 2.61 0 0 0 0.013 -0.337c0 -1.207 "
    "-0.987 -1.672 -1.92 -1.672 -0.934 0 -1.775 0.784 -1.777 1.672 0.016 1.027 0 2.046 0 "
    "3.07l1.732 -0.012c1.352 -0.028 1.368 2.009 0.016 1.998l-1.748 0.013c-0.004 0.826 0.006 "
    "0.677 0.002 1.093 0 0 0.015 1.01 -0.016 1.776 -0.209 2.25 -2.124 4.046 -4.424 4.046 "
    "-2.438 0 -4.448 -1.993 -4.448 -4.437 0.073 -2.515 2.078 -4.492 4.603 -4.469l1.409 "
    "-0.01v1.996l-1.409 0.013h-0.007c-1.388 0.04 -2.577 0.984 -2.6 2.47a2.438 2.438 0 0 0 "
    "2.452 2.439c1.356 0 2.441 -0.987 2.441 -2.437l-0.001 -7.557c0 -0.14 0.005 -0.252 0.02 "
    "-0.407 0.23 -1.848 1.883 -3.256 3.754 -3.256z"
)

NEOFETCH_W = 780
NEOFETCH_H = 220


def _text(
    x: int,
    y: int,
    value: str,
    *,
    size: float = 11.5,
    fill: str = "#d1d5db",
    weight: Optional[str] = None,
    anchor: Optional[str] = None,
) -> str:

    attrs = [
        f'x="{x}"',
        f'y="{y}"',
        "font-family=\"'SF Mono',Menlo,Consolas,monospace\"",
        f'font-size="{size}"',
        f'fill="{fill}"',
    ]

    if weight:
        attrs.append(f'font-weight="{weight}"')

    if anchor:
        attrs.append(f'text-anchor="{anchor}"')

    escaped = (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    return f"  <text {' '.join(attrs)}>{escaped}</text>"


def _fetch_profile_totals() -> dict[str, Any]:
    """Stars, forks, repo count and follower count of the account."""

    profile = _get_json(f"{API_ROOT}/users/{USERNAME}")
    repos = _get_json(
        f"{API_ROOT}/users/{USERNAME}/repos",
        params={"per_page": 100, "type": "owner"},
    )

    stars = 0
    forks = 0
    language_counts: dict[str, int] = {}

    for repo in repos:
        stars += int(repo.get("stargazers_count") or 0)
        forks += int(repo.get("forks_count") or 0)
        language = repo.get("language")
        if language:
            language_counts[language] = language_counts.get(language, 0) + 1

    top_language = ""

    if language_counts:
        top_language = max(
            language_counts.items(),
            key=lambda item: item[1],
        )[0]

    return {
        "repos": int(profile.get("public_repos") or len(repos)),
        "followers": int(profile.get("followers") or 0),
        "following": int(profile.get("following") or 0),
        "stars": stars,
        "forks": forks,
        "top_language": top_language,
    }


def _read_contribution_snapshot() -> dict[str, Any]:
    """Commit / streak numbers from the stats snapshot, if it exists."""

    raw = _read_text(DATA_JSON_PATH)

    if not raw:
        return {}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    return payload if isinstance(payload, dict) else {}


def render_neofetch_card(
    totals: dict[str, Any],
    contribution: dict[str, Any],
) -> str:

    commits = int(contribution.get("commits") or 0)
    streak = int(contribution.get("currentStreak") or 0)
    total_contributions = int(
        contribution.get("totalContributions") or 0
    )

    repos = int(totals.get("repos") or 0)
    stars = int(totals.get("stars") or 0)
    followers = int(totals.get("followers") or 0)
    top_language = totals.get("top_language") or "—"

    system_rows = [
        ("OS", "Fedora Linux 40 x86_64"),
        ("Kernel", "6.8.11-300.fc40.x86_64"),
        ("Shell", "zsh 5.9"),
        ("WM", "Hyprland"),
        ("Editor", "Neovim"),
        ("Terminal", "Kitty"),
    ]

    focus_rows = [
        "Data Science · ML",
        "Python-first workflows",
        "Deep Learning research",
    ]

    github_rows = [
        ("Repos", str(repos)),
        ("Stars", str(stars)),
        ("Followers", str(followers)),
        ("Commits", str(commits)),
        ("Contribs", str(total_contributions)),
        ("Streak", f"{streak} days"),
        ("Top Lang", top_language),
    ]

    lines: list[str] = []

    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {NEOFETCH_W} {NEOFETCH_H}" '
        f'width="{NEOFETCH_W}" height="{NEOFETCH_H}">'
    )

    lines.append("  <defs>")
    lines.append(
        '    <radialGradient id="badge" cx="35%" cy="28%" r="72%">'
    )
    lines.append('      <stop offset="0%"   stop-color="#3c3c3c"/>')
    lines.append('      <stop offset="35%"  stop-color="#1f1f1f"/>')
    lines.append('      <stop offset="100%" stop-color="#090909"/>')
    lines.append("    </radialGradient>")
    lines.append(
        '    <radialGradient id="rimHi" cx="30%" cy="25%" r="55%">'
    )
    lines.append(
        '      <stop offset="0%"   stop-color="#ffffff" stop-opacity="0.14"/>'
    )
    lines.append(
        '      <stop offset="100%" stop-color="#ffffff" stop-opacity="0"/>'
    )
    lines.append("    </radialGradient>")
    lines.append("    <clipPath id=\"badgeClip\">")
    lines.append(
        '      <rect x="20" y="38" width="104" height="122" rx="28"/>'
    )
    lines.append("    </clipPath>")
    lines.append("  </defs>")

    # Terminal chrome.
    lines.append("  <!-- ── TERMINAL WINDOW ── -->")
    lines.append(
        f'  <rect width="{NEOFETCH_W}" height="{NEOFETCH_H}" rx="10" fill="#191919"/>'
    )
    lines.append(
        f'  <rect width="{NEOFETCH_W}" height="27" rx="10" fill="#232323"/>'
    )
    lines.append(
        f'  <rect y="15" width="{NEOFETCH_W}" height="12" fill="#232323"/>'
    )
    lines.append(
        '  <circle cx="16" cy="13.5" r="5" fill="#3d3d3d"/>'
    )
    lines.append(
        '  <circle cx="31" cy="13.5" r="5" fill="#3d3d3d"/>'
    )
    lines.append(
        '  <circle cx="46" cy="13.5" r="5" fill="#3d3d3d"/>'
    )
    lines.append(
        _text(
            NEOFETCH_W // 2,
            18,
            "ayush@github — neofetch",
            size=10.5,
            fill="#4f4f4f",
            anchor="middle",
        )
    )

    # Fedora badge.
    lines.append("  <!-- ── FEDORA BADGE ── -->")
    lines.append(
        '  <rect x="20" y="38" width="104" height="122" rx="28" fill="url(#badge)"/>'
    )
    lines.append(
        '  <rect x="20" y="38" width="104" height="122" rx="28"'
    )
    lines.append(
        '    fill="none" stroke="#3a3a3a" stroke-width="1" opacity="0.7"/>'
    )
    lines.append(
        '  <rect x="20" y="38" width="104" height="122" rx="28"'
    )
    lines.append(
        '    fill="url(#rimHi)" clip-path="url(#badgeClip)"/>'
    )
    lines.append(
        '  <g transform="translate(34.8, 62.6) scale(3.2)" opacity="0.55">'
    )
    lines.append(f'    <path fill="#000" d="{FEDORA_PATH}"/>')
    lines.append("  </g>")
    lines.append(
        '  <g transform="translate(33.6, 60.6) scale(3.2)">'
    )
    lines.append(f'    <path fill="#cecece" d="{FEDORA_PATH}"/>')
    lines.append("  </g>")

    # Column 1: system.
    lines.append("  <!-- ── NEOFETCH CONTENT ── -->")
    lines.append(
        _text(
            146,
            58,
            "ayush@github",
            size=13.5,
            weight="bold",
            fill="#e8e8e8",
        )
    )
    lines.append(
        '  <line x1="146" y1="63" x2="400" y2="63" stroke="#2e2e2e" stroke-width="1"/>'
    )

    for index, (key, value) in enumerate(system_rows):
        y = 81 + index * 16
        lines.append(_text(146, y, key, fill="#6b7280"))
        lines.append(_text(197, y, "→", fill="#404040"))
        lines.append(_text(218, y, value))

    # Column 2: focus.
    lines.append(_text(414, 81, "Focus", fill="#4b5563"))
    for index, value in enumerate(focus_rows):
        lines.append(_text(414, 97 + index * 16, value, fill="#9ca3af"))

    # Column 3: live GitHub numbers.
    lines.append(_text(586, 81, "GitHub", fill="#4b5563"))
    for index, (key, value) in enumerate(github_rows):
        y = 97 + index * 16
        lines.append(_text(586, y, key, fill="#6b7280"))
        lines.append(_text(660, y, value, fill="#9ca3af"))

    # Swatches.
    swatches = ["#191919", "#374151", "#4b5563", "#6b7280", "#9ca3af", "#d1d5db", "#f3f4f6"]
    for index, colour in enumerate(swatches):
        x = 146 + index * 16
        lines.append(
            f'  <rect x="{x}" y="196" width="13" height="10" rx="2" '
            f'fill="{colour}" stroke="#3a3a3a" stroke-width="0.8"/>'
        )

    lines.append("</svg>")

    return "\n".join(lines) + "\n"


def update_neofetch_card() -> bool:

    try:
        totals = _fetch_profile_totals()
        contribution = _read_contribution_snapshot()
        svg = render_neofetch_card(totals, contribution)

        current = _read_text(NEOFETCH_PATH)

        if current == svg:
            log.info("neofetch_card already up to date")
            return False

        _atomic_write(NEOFETCH_PATH, svg)
        log.info(
            "neofetch_card updated: %s repos, %s stars, %s followers",
            totals.get("repos"),
            totals.get("stars"),
            totals.get("followers"),
        )
        return True

    except Exception as exc:
        log.warning("neofetch_card not updated: %s", exc)
        return False


# ============================================================================
# ASSET: index.html fallback snapshot
# ============================================================================

def update_index_fallback() -> bool:
    """
    Refresh the hardcoded snapshot that index.html falls back to when
    data.json cannot be fetched (file:// pages are blocked by CORS).

    The whole `githubData` object is regenerated from data.json, so it
    can never drift from the real numbers.
    """

    try:
        contribution = _read_contribution_snapshot()

        if not contribution:
            return False

        content = _read_text(INDEX_PATH)

        if content is None:
            return False

        def number(key: str) -> int:
            return int(contribution.get(key) or 0)

        def quote(key: str) -> str:
            return json.dumps(contribution.get(key) or "")

        languages = contribution.get("languages") or []

        language_lines = []

        for language in languages:
            pct = language.get("pct", 0)
            pct_text = (
                str(int(pct))
                if float(pct).is_integer()
                else f"{float(pct):g}"
            )
            language_lines.append(
                f'    {{ name: {json.dumps(language.get("name", ""))}, '
                f"pct: {pct_text} }}"
            )

        if language_lines:
            languages_block = ",\n".join(language_lines)
        else:
            languages_block = ""

        block = (
            "var githubData = {\n"
            f"  asOf: {quote('asOf')},\n"
            f"  since: {quote('since')},\n"
            f"  totalContributions: {number('totalContributions')},\n"
            f"  commits: {number('commits')},\n"
            f"  pullRequests: {number('pullRequests')},\n"
            f"  activeDays: {number('activeDays')},\n"
            f"  currentStreak: {number('currentStreak')},\n"
            f"  longestStreak: {number('longestStreak')},\n"
            f"  repos: {number('repos')},\n"
            f"  languages: [\n{languages_block}\n  ]\n"
            "};"
        )

        pattern = re.compile(
            r"var githubData\s*=\s*\{.*?\n\};",
            re.DOTALL,
        )

        updated, count = pattern.subn(
            lambda _: block,
            content,
            count=1,
        )

        if count != 1:
            log.warning("index.html: githubData block not found")
            return False

        if updated == content:
            log.info("index.html fallback already up to date")
            return False

        _atomic_write(INDEX_PATH, updated)
        log.info("index.html fallback snapshot updated")
        return True

    except Exception as exc:
        log.warning("index.html fallback not updated: %s", exc)
        return False


# ============================================================================
# ENTRY POINT
# ============================================================================

def sync_all() -> int:
    """
    Run every updater. Returns the number of surfaces that changed.
    """

    results = {
        "recent_repos": update_recent_repos(),
        "pinned_repos": update_pinned_repos(),
        "achievements": update_achievements(),
        "neofetch_card": update_neofetch_card(),
        "index_fallback": update_index_fallback(),
    }

    changed = sum(1 for value in results.values() if value)

    # The freshness stamp follows real publishes. Bumping it on every
    # run would create an empty commit every five minutes and bury the
    # history, so it changes when a surface changes - or when the footer
    # template itself changed and the README still carries the old one.
    if changed or sync_stamp_needs_refresh():
        results["sync_stamp"] = update_sync_stamp()

    for name, value in results.items():
        log.info(
            "%-16s %s",
            name,
            "updated" if value else "unchanged",
        )

    log.info("readme sync finished: %s surface(s) changed", changed)

    return changed


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    sync_all()
