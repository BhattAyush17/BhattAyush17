# Running GitHub Stats Generator Locally

## 1. Prerequisites

### Get a GitHub Personal Access Token
1. Go to https://github.com/settings/tokens
2. Click **Generate new token** → **Generate new token (classic)**
3. Give it a name like "stats-local"
4. Check these scopes:
   - ✅ `public_repo` (read public repos)
   - ✅ `read:user` (read user profile)
5. Copy the token (you'll only see it once)

### Clone & Install
```bash
# Navigate to your repo
cd /path/to/BhattAyush17-main

# Install Python dependencies
pip install -r scripts/requirements.txt

# For macOS/Linux, if you hit matplotlib issues:
pip install --upgrade matplotlib --break-system-packages
```

## 2. Run the Script Locally

### Option A: With Your Token (Full Data)
```bash
export GH_STATS_TOKEN="ghp_your_actual_token_here"
python scripts/generate_stats.py
```

**Expected output:**
```
17:45:23 [INFO] 🚀 Starting robust stats generator — 2026-08-26T17:45:23.456789+00:00
17:45:24 [INFO] ✅ Live GitHub data fetched and cached.
17:45:25 [INFO] ✅ github-stats.svg written.
17:45:26 [INFO] ✅ languages.svg written.
17:45:27 [INFO] ✅ streak.svg written.
17:45:28 [INFO] ✅ contribution-graph.svg downloaded from Vercel.
17:45:30 [INFO] ✅ summary-stats.svg downloaded.
17:45:32 [INFO] ✅ summary-repos-per-language.svg downloaded.
17:45:34 [INFO] ✅ summary-most-commit-language.svg downloaded.
17:45:36 [INFO] ✅ summary-productive-time.svg downloaded.
17:45:37 [INFO] 🏆 Scraping GitHub achievements…
17:45:40 [INFO] ✅ Achievements injected into README.md.
17:45:40 [INFO] ✨ Stats generation complete.
```

### Option B: Without Token (Cache Only)
If you don't have a token or want to test fallback behavior:
```bash
python scripts/generate_stats.py
```
It will try to use the cache from `assets/cache/data.json` (created on first successful run). If the cache is fresh (< 6 hours old), it'll use real data. Otherwise it'll fail gracefully.

## 3. Inspect the Output

### Check Generated SVGs
```bash
# List what was generated
ls -lh assets/stats/

# View file sizes (healthy SVGs are > 1 KB)
du -h assets/stats/*.svg

# Validate SVG syntax (should contain <svg xmlns>)
head -1 assets/stats/github-stats.svg
head -1 assets/stats/languages.svg
```

### Open in Browser
```bash
# macOS
open assets/stats/github-stats.svg
open assets/stats/streak.svg
open assets/stats/languages.svg

# Linux
xdg-open assets/stats/github-stats.svg

# Or just drag any .svg file into your browser
```

### Check the Cache
```bash
cat assets/cache/data.json | python -m json.tool | head -50
```
Should show your real GitHub stats with a timestamp.

## 4. Test Scenarios

### Scenario 1: Fresh Fetch + Real Render
```bash
export GH_STATS_TOKEN="your_token"
rm -f assets/cache/data.json  # force fresh fetch
python scripts/generate_stats.py
# ✅ Should fetch live, render all cards, cache the data
```

### Scenario 2: Use Stale Cache
```bash
unset GH_STATS_TOKEN
python scripts/generate_stats.py
# ✅ Should load from cache (even if 7+ hours old) and render
```

### Scenario 3: No Token, No Cache (Failure)
```bash
unset GH_STATS_TOKEN
rm -f assets/cache/data.json
python scripts/generate_stats.py
# ❌ Should exit with error message, keeping old SVGs untouched
```

### Scenario 4: Broken External Service (e.g., Vercel Down)
```bash
export GH_STATS_TOKEN="your_token"
python scripts/generate_stats.py
# ✅ If Vercel API times out, should fall back to local heatmap
# ✅ Other cards still render normally
```

## 5. Troubleshooting

### "No module named matplotlib"
```bash
pip install matplotlib>=3.8.0 --break-system-packages
```

### "Matplotlib can't find X11" (Linux)
```bash
sudo apt-get install -y libcairo2-dev
pip install cairosvg>=2.7.0
```

### SVGs look broken/blank
1. Check file size: `ls -lh assets/stats/github-stats.svg`
   - Should be > 2 KB
2. Check for error markers in the SVG: `grep -i "unavailable\|error" assets/stats/github-stats.svg`
3. Check logs for what failed: look at the [ERROR] lines in output
4. Re-run with your token to force a fresh render

### Token not working
- Confirm it has `public_repo` + `read:user` scopes
- Test: `curl -H "Authorization: Bearer your_token" https://api.github.com/user`
  - Should return your GitHub user JSON, not `{"message": "Bad credentials"}`

## 6. Before Committing

1. **Run locally and verify visually:**
   ```bash
   export GH_STATS_TOKEN="your_token"
   python scripts/generate_stats.py
   open assets/stats/*.svg  # inspect each card
   ```

2. **Check cache was created:**
   ```bash
   ls -lh assets/cache/data.json
   ```

3. **Commit only the SVGs, not the cache:**
   ```bash
   git add assets/stats/*.svg README.md
   git add -u assets/  # stage deletions if any
   git status
   git commit -m "chore: update README stats [generate_stats.py]"
   ```

4. **Do NOT commit `assets/cache/data.json`** — add it to `.gitignore`:
   ```bash
   echo "assets/cache/" >> .gitignore
   git add .gitignore
   git commit -m "chore: ignore cache files"
   ```

## 7. Watch Logs in Real Time

To see exactly what's happening as it runs:
```bash
export GH_STATS_TOKEN="your_token"
python -u scripts/generate_stats.py 2>&1 | tee stats-run.log
```

Then inspect the log:
```bash
cat stats-run.log | grep "ERROR\|WARNING\|✅"
```

---

**Next:** Once you've run it locally and visually confirmed the stats look correct, the GitHub Actions workflow will do the same automatically every 6 hours. 🎉
