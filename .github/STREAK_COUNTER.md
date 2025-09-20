# GitHub Streak Counter

This repository uses an automated system to track and display the current GitHub activity streak in the README.md file.

## How it works

1. **Daily Schedule**: The workflow runs every day at 00:30 UTC
2. **Manual Trigger**: Can be triggered manually via GitHub Actions tab
3. **Activity Tracking**: Monitors various GitHub activities including:
   - Push events
   - Pull request events  
   - Issue events
   - Create/delete events
   - Commit comments

## Files

- `.github/workflows/update-streak.yml` - GitHub Actions workflow
- `.github/scripts/update_streak.py` - Python script that calculates the streak
- `.github/scripts/test_streak.py` - Test script for validation

## Streak Calculation Logic

The streak counter counts consecutive days with GitHub activity. It:
- Fetches recent events via GitHub API
- Filters for relevant activity types
- Calculates consecutive days from today backwards
- Accounts for timezone differences (up to 2 days tolerance)
- Updates the README.md between `<!--STREAK_COUNT-->` markers

## Display Location

The streak is displayed in the GitHub Stats section of README.md:

```markdown
<span style="font-size:2em; color:#39d353;"><!--STREAK_COUNT-->0<!--STREAK_COUNT--></span>
```

The counter automatically updates the number between the comment markers while preserving all other formatting.