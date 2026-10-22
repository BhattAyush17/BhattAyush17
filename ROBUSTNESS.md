# Robustness & Accuracy Guarantees

This stats generator is designed to **never show false data** — only real GitHub statistics, always self-healing on failure.

## Three Core Principles

### 1️⃣ Real Data Priority
```
Live (fresh) Data  >  Cached Data  >  Old File  >  Exit Cleanly (no fake data)
```

**We'll never do this:**
- ❌ Replace a real SVG with fake numbers (`{"Python": 5, "C++": 2}` as placeholders)
- ❌ Overwrite accurate old data with a generic "unavailable" card
- ❌ Show estimated numbers or best-guesses

**We do this instead:**
- ✅ If live fetch fails → use cache (even if 12+ hours old)
- ✅ If cache fails → keep the last real SVG you had
- ✅ If no data exists at all → exit non-zero without touching the cards
- ✅ Badge every card with its source (`live`, `cached · <age>`, `stale · <age>`)

### 2️⃣ Fail-Open, Not Fail-Closed
When something breaks:

| Scenario | Old Behavior | New Behavior |
|---|---|---|
| Language detection fails | Show fake `{"Python": 5, ...}` | Skip rendering, keep existing file |
| GitHub API timeout | Crash or show partial garbage | Preserve last good version |
| Hourly lookup fails | Crash the run | Keep the previous `commit-hours.svg` and label it `snapshot` |

### 3️⃣ Auditable & Detectable
Every run logs exactly what data was used:

```
✅ Live GitHub data fetched and cached.
Cards written: assets/stats/github-cards.svg
```

There are no placeholder files: a failed run leaves the previous cards on disk
and the freshness badge in the card header tells you which source they came
from.

---

## Self-Healing Workflow

### How It Works
1. **`sync-readme.yml`** runs every 5 minutes (GitHub's shortest
   scheduled interval) and regenerates every GitHub-derived surface in
   one pass:
   - stats cards, commit-activity chart and streak (live GraphQL)
   - recent repos, pinned repos and achievements (live REST / profile scrape)
   - the neofetch card, with live GitHub numbers baked into the SVG
   - the contribution snake (from the live GitHub grid)
   - the freshness stamp in the README
2. **`fallback-healthcheck.yml`** audits that every expected asset exists,
   that every managed README section is present, and that the image cache
   busters match the file contents — it fails loudly instead of silently
   shipping a broken profile
3. Both publish through `scripts/publish_assets.sh`, so a fix lands
   on `main` even when another workflow pushed in the meantime

There is exactly **one** writer. The three overlapping workflows that
used to exist (`generate-readme-assets`, `generate-snake`,
`streak-healthcheck`) duplicated each other's API calls, raced for the
same branch, and left the two README repo tables frozen since
2026-08-27.

### Example: Language Fetch Fails
```bash
Time 14:00 — sync-readme runs
→ The secondary repository query times out
→ Script logs: "Language data unavailable - keeping the existing cards"
→ The three cards are still written from contribution data
→ languages card falls back to the committed data.json snapshot
→ the README tables are still refreshed from the live API
→ if nothing changed upstream, no commit is created at all

Time 14:30 — fallback-healthcheck runs (scheduled)
→ Audits github-cards.svg and commit-hours.svg
→ Both exist and contain <svg>
→ Audits recent_repos / pinned_repos / achievements / sync_stamp
→ Status: ✅ HEALTHY, no action needed
```

### Example: Cache Corrupted
```bash
Time 15:00 — sync-readme runs
→ Token is expired, cache doesn't exist
→ Script logs: "Both live API fetch and local cache failed"
→ Script exits with error code 1
→ All SVGs are UNCHANGED from previous run

Time 15:30 — fallback-healthcheck runs
→ Audits all SVGs
→ github-stats.svg exists and has content (from last run)
→ Status: ✅ HEALTHY, no action needed
→ Next cycle, if token is fixed, fresh data will flow in
```

---

## For Local Testing

### See Exactly What's Being Rendered
```bash
export GH_STATS_TOKEN="your_token"
python scripts/generate_stats.py 2>&1 | grep -E "✅|❌|ERROR|NEVER|real data"
```

Expected output for a healthy run:
```
[INFO] LIVE SUCCESS: 941 ms
[INFO] Cards written: assets/stats/github-cards.svg
[INFO] data.json written: streak=4 repos=25 languages=8
[INFO] SOURCE: graphql_api
[INFO] FRESHNESS: just now
```

### Test Fallback Behavior
```bash
# Simulate API failure (no token, no cache)
unset GH_STATS_TOKEN
rm -f assets/cache/bhattayush17-github.json
python scripts/generate_stats.py

# Expected: Script fails gracefully, keeping old SVGs
# ❌ Cannot fetch any data
# Exit code: 1
```

### Test Cache Fallback
```bash
# Trigger a broken cache and remove the live path
echo '{"invalid": "json"}' > assets/cache/bhattayush17-github.json
unset GH_STATS_TOKEN
python scripts/generate_stats.py

# Script will try to parse the cache, fail, then exit without touching
# the existing cards (exit code 1).
```

---

## Data Sources & Freshness

| Card | Source | Refreshed | Fallback |
|---|---|---|---|
| **github-cards.svg** | Composed from the three cards below | Every run | Keep old file |
| **commit-hours.svg** | GitHub commit search API (real commit times, last year) | Every run | Last committed `hourly.json`, labelled `snapshot` |
| **github-stats.svg** | GitHub GraphQL API | Every run | Cached data (6h TTL), then old file |
| **streak.svg** | GitHub GraphQL (contributions calendar) | Every run | Cached data, then old file |
| **languages.svg** | GitHub GraphQL (repo primary languages) | Every run | Cached data or `data.json`, then old file |
| **snake SVG** | `Platane/snk` (GitHub contribution grid) + `scripts/finalize_snake.py` | Every 12 h | Keep old file, retry next cycle |

**Cache policy:**
- Fetched data cached to `assets/cache/bhattayush17-github.json` with timestamp
- Cache is "fresh" if < 6 hours old → use immediately
- Cache is "stale" if > 6 hours old → but still use if live fetch fails (graceful degradation)
- Cache is re-written on every successful live fetch

---

## Streak Rules (and why they are timezone aware)

GitHub buckets contributions by the **user's own timezone**, not by UTC. The
calendar therefore ends on the user's "today", which can be one day ahead of
or behind the UTC date.

| Situation | Calendar ends | Streak is anchored on |
|---|---|---|
| User ahead of UTC (e.g. UTC+05:30) | `utc_today + 1` | The last day of the calendar |
| User behind UTC (e.g. UTC-07:00) | `utc_today - 1` | The last day of the calendar |
| Payload is stale (calendar older than that) | `utc_today - 2` or older | The UTC date → streak reports `0` |

Rules applied by `scripts/generate_stats.py`:

- The current streak starts on the anchor day when it has contributions,
  otherwise on the day before it — a streak stays alive until the end of the
  next day.
- A **missing** date is never counted as active.
- The longest streak requires exact one-day adjacency and is limited to the
  ~12 months GitHub exposes (the card labels it `Longest (12 mo)`).
- A payload whose newest day is more than one day behind the UTC date is
  treated as stale: the current streak is reported as `0` instead of an
  outdated number.

---

## Publishing Assets Without Conflicts

All asset workflows push to the same branch and share the
`github-analytics` concurrency group, so a push can race with another
workflow's commit.

`scripts/publish_assets.sh` handles that safely:

1. `git add` + `git commit` **first** (the working tree becomes clean)
2. `git fetch origin main` + `git rebase origin/main`
3. `git push origin HEAD:main`

Rebasing **after** the commit is the important part: `git pull --rebase` on a
dirty working tree aborts with
`error: cannot pull with rebase: You have unstaged changes`, which is what
kept the snake animation and the streak card stale for weeks.

If the rebase conflicts, the freshly generated files win and the commit is
re-created on top of the newest `main`.

---

## Known Limitations & Mitigations

### GitHub API Rate Limiting
- **Limit:** 5,000 requests/hour per token
- **Mitigation:** Cache + retry logic. If rate-limited, uses cache (6h+ old is fine)

### Repo Pagination
- **Limit:** Query fetches first 100 repos only
- **Impact:** Counts for stars/forks/languages only include first 100
- **Mitigation:** If you have 100+ repos, consider increasing `first:` parameter in `_GQL_QUERY`

### External Service Downtime
- **Services:** GitHub GraphQL API
- **Mitigation:** use the committed cache, keep the old cards, and label them
  `cached · <age>` so the stale source is visible

### Network Timeouts
- **Risk:** Partial data corruption
- **Mitigation:** All API calls have retry + back-off. Temporary failures don't touch files.

---

## CI/CD Integration

### Required Secret
Set `GH_STATS_TOKEN` as a repository secret:
1. Go to **Settings** → **Secrets and variables** → **Actions**
2. Create new secret `GH_STATS_TOKEN` with your PAT
3. Scopes needed: `public_repo`, `read:user`

### Workflows
- **`sync-readme.yml`** — the single writer. Runs every 5 minutes and on
  every push to `main`, and refreshes every GitHub-derived surface
- **`fallback-healthcheck.yml`** — read-only verifier. Runs every 30
  minutes and checks that the assets exist, the README sections are
  present, and the image cache busters match the file contents

All of them publish through `scripts/publish_assets.sh` and share the
`github-analytics` concurrency group, so they queue instead of racing.

### Managed README Sections

`scripts/readme_sync.py` owns every block between `START_SECTION:` and
`END_SECTION:` markers, plus the neofetch card and the `index.html`
fallback snapshot:

| Marker | Source |
|--------|--------|
| `recent_repos` | `GET /users/{user}/repos?sort=pushed` |
| `pinned_repos` | GraphQL `pinnedItems` |
| `achievements` | profile sidebar, `img.achievement-badge-sidebar` |
| `sync_stamp` | local clock, only bumped when data actually changed |

Every updater is fail-safe: a failed fetch logs a warning and leaves the
previous content in place. The README is never left half-written.

### Image Cache Busters

`scripts/bump_versions.py` gives every local image in the README a
`?v=<sha256 of the file>` parameter. GitHub caches images by URL, so the
version has to change when the content does — but a timestamp would
change on *every* run and produce an empty commit every 5 minutes. A
content hash changes exactly when the content changes, so a run that
finds nothing new upstream produces no commit at all.

The same rule applies to `assets/stats/data.json` and the contribution
cache: their timestamps only move when the data inside them moves.

---

## The Automation Feedback Loop

A profile that publishes its own statistics has a classic trap: the
automation commits, the commit shows up in the contribution calendar,
the calendar changes, the next run sees a different number and commits
again — forever.

Three things prevent it here.

**1. The bot's commits are not counted by GitHub.** The publish step
rebases the bot's commits onto the newest `main`, which rewrites their
SHA, and a rewritten commit loses its contribution credit. Measured over
this repository's history: 23 automation commits on a day contributed
**0** to the calendar, while the 9 human commits on the same day
contributed exactly 9. The numbers are therefore reported exactly as
GitHub reports them — no subtraction, no guessing.

**2. The head of the profile repository is tracked.** Every run reads
the recent history of the profile repo and remembers the head SHA in the
cache. When every commit made since the previous run was the bot's, the
contribution numbers cannot have changed, so the previous numbers are
kept and nothing is published.

**3. Everything is idempotent.** A run that finds no change upstream
produces an identical tree: no diff, no commit, no cache invalidation.

The result: the workflow reacts to real activity and stays completely
silent otherwise.

### Monitoring
Check the **Actions** tab for any failures:
- 🟢 Green: All assets healthy
- 🟡 Yellow: Some assets re-generated (self-heal triggered)
- 🔴 Red: Critical error (token expired, GitHub API down, etc.)

---

## Troubleshooting Checklist

**Problem:** SVGs are blank/corrupted
```bash
# Solution 1: Check file size
ls -lh assets/stats/github-stats.svg
# Should be > 2 KB

# Solution 2: Check for error logs
cat stats-run.log | grep ERROR

# Solution 3: Force fresh render
rm -f assets/cache/bhattayush17-github.json
export GH_STATS_TOKEN="your_token"
python scripts/generate_stats.py
```

**Problem:** "Cannot fetch any data"
```bash
# Solution 1: Check token
curl -H "Authorization: Bearer $GH_STATS_TOKEN" https://api.github.com/user
# Should return your GitHub user, not {"message": "Bad credentials"}

# Solution 2: Check cache
cat assets/cache/bhattayush17-github.json | python -m json.tool | head -20

# Solution 3: Manually run with logging
python -u scripts/generate_stats.py 2>&1 | tee debug.log
```

**Problem:** Cards look stale on the profile
```bash
# GitHub caches README images by URL. The ?v= parameter is a hash of the
# file content, so it changes exactly when the content changes. If you
# published manually, recompute it:
python scripts/bump_versions.py

# Check that it is in sync (exit code 1 means it is not):
python scripts/bump_versions.py --check

# If the cards are genuinely unchanged, check the run log for
# "SOURCE: cache" or "FRESHNESS: cached" and verify GH_STATS_TOKEN is set.
```

---

## Summary

✅ **What you get:**
- Real GitHub statistics, always
- Automatic recovery from API failures
- Cache-based fallback (graceful degradation)
- Self-healing CI/CD (no manual fixes needed)
- Full audit trail in logs

❌ **What you don't get:**
- Fake data, guesses, or estimates
- Broken SVGs replacing working ones
- Silent failures (errors are logged)
- Confusing "loading" states

---

**Questions?** Check `LOCAL_SETUP.md` for local testing, or review the logs in GitHub Actions.
