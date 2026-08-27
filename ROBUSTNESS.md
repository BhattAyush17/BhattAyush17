# Robustness & Accuracy Guarantees

This stats generator is designed to **never show false data** — only real GitHub statistics, always self-healing on failure.

## Three Core Principles

### 1️⃣ Real Data Priority
```
Live (fresh) Data  >  Cached Data  >  Old File  >  Placeholder  >  Invent Numbers
```

**We'll never do this:**
- ❌ Replace a real SVG with fake numbers (`{"Python": 5, "C++": 2}` as placeholders)
- ❌ Overwrite accurate old data with a generic "unavailable" card
- ❌ Show estimated numbers or best-guesses

**We do this instead:**
- ✅ If live fetch fails → use cache (even if 12+ hours old)
- ✅ If cache fails → keep the last real SVG you had
- ✅ Only show "data unavailable" placeholder if that file never existed
- ✅ Mark every placeholder with a hidden marker so self-heal knows to retry

### 2️⃣ Fail-Open, Not Fail-Closed
When something breaks:

| Scenario | Old Behavior | New Behavior |
|---|---|---|
| Vercel graph API down | Show placeholder, abandon real data | Use local heatmap from real GitHub data |
| Language detection fails | Show fake `{"Python": 5, ...}` | Skip rendering, keep existing file |
| Summary card download fails | Replace with placeholder | Keep old real card, try again next cycle |
| Network timeout mid-render | Crash or show partial garbage | Preserve last good version |

### 3️⃣ Auditable & Detectable
Every run logs exactly what data was used:

```
✅ Live GitHub data fetched and cached.
Rendering stats with real data: 42 stars, 15 forks, 238 commits
✅ github-stats.svg written with real data.
```

Placeholders are marked so the health-check workflow knows to keep retrying:
```html
<!-- PLACEHOLDER: WILL_RETRY -->
<svg>... data unavailable — will retry ...</svg>
```

---

## Self-Healing Workflow

### How It Works
1. **Runs every 6 hours** via `fallback-healthcheck.yml`
2. **Audits each SVG** for:
   - File exists and has content (not empty)
   - SVG is real data, not a temporary placeholder
3. **Only regenerates if broken** — doesn't waste API calls on healthy assets
4. **Commits fixes automatically** with message `fix: self-heal broken README assets [skip ci]`

### Example: Vercel is Down
```bash
Time 14:00 — generate-readme-assets runs
→ Vercel API is down
→ Script logs: "Vercel graph fetch failed — generating local heatmap"
→ Renders contribution heatmap from real GitHub calendar data
→ SVG is GOOD (real data via fallback)

Time 20:30 — fallback-healthcheck runs (scheduled)
→ Audits contribution-graph.svg
→ File exists, contains real heatmap, no PLACEHOLDER marker
→ Status: ✅ HEALTHY, no action needed
```

### Example: Cache Corrupted
```bash
Time 15:00 — generate-readme-assets runs
→ Token is expired, cache doesn't exist
→ Script logs: "Both live API fetch and local cache failed"
→ Script exits with error code 1
→ All SVGs are UNCHANGED from previous run

Time 21:30 — fallback-healthcheck runs
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
Policy: NEVER show false data...
✅ Live GitHub data fetched and cached.
Rendering stats with real data: 42 stars, 15 forks, 238 commits
✅ github-stats.svg written with real data.
Rendering streak with real data: current=7, longest=45, total=892
✅ streak.svg written with real data.
...
✨ Stats generation complete. All SVGs contain REAL data...
```

### Test Fallback Behavior
```bash
# Simulate API failure (no token, no cache)
unset GH_STATS_TOKEN
rm -f assets/cache/data.json
python scripts/generate_stats.py

# Expected: Script fails gracefully, keeping old SVGs
# ❌ Cannot fetch any data
# Exit code: 1
```

### Test Placeholder Detection
```bash
# Trigger a broken render (corrupt cache)
echo '{"invalid": "json"}' > assets/cache/data.json
python scripts/generate_stats.py

# Script will try to parse, fail, then keep old SVGs
# ✅ Old data preserved
```

---

## Data Sources & Freshness

| Card | Source | Refreshed | Fallback |
|---|---|---|---|
| **github-stats.svg** | GitHub GraphQL API | Every run | Cached data (6h TTL), then old file |
| **streak.svg** | GitHub GraphQL (contributions calendar) | Every run | Cached data, then old file |
| **languages.svg** | GitHub GraphQL (repo primary languages) | Every run | Skip (never fake), keep old file |
| **contribution-graph.svg** | Vercel activity graph OR local heatmap | Every run | Local heatmap (real data), then old file |
| **summary-\*.svg** | Vercel summary cards | Every run | Keep old file, retry next cycle |

**Cache policy:**
- Fetched data cached to `assets/cache/data.json` with timestamp
- Cache is "fresh" if < 6 hours old → use immediately
- Cache is "stale" if > 6 hours old → but still use if live fetch fails (graceful degradation)
- Cache is re-written on every successful live fetch

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
- **Services:** Vercel (activity graph, summary cards)
- **Mitigation:** 
  - For activity graph: fallback to local heatmap (still real data)
  - For summary cards: keep old version, retry next cycle

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
- **`generate-readme-assets.yml`** — runs on schedule (usually nightly) to fetch fresh data
- **`fallback-healthcheck.yml`** — runs every 6 hours to audit and self-heal

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
rm -f assets/cache/data.json
export GH_STATS_TOKEN="your_token"
python scripts/generate_stats.py
```

**Problem:** "Cannot fetch any data"
```bash
# Solution 1: Check token
curl -H "Authorization: Bearer $GH_STATS_TOKEN" https://api.github.com/user
# Should return your GitHub user, not {"message": "Bad credentials"}

# Solution 2: Check cache
cat assets/cache/data.json | python -m json.tool | head -20

# Solution 3: Manually run with logging
python -u scripts/generate_stats.py 2>&1 | tee debug.log
```

**Problem:** Placeholder SVGs keep appearing
```bash
# Check if placeholder marker exists
grep "PLACEHOLDER: WILL_RETRY" assets/stats/*.svg

# The health-check should have re-triggered. If not:
# - Verify GH_STATS_TOKEN secret is set in GitHub Settings
# - Manually trigger: Actions → fallback-healthcheck → Run workflow
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
