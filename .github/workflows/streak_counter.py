import os
import sys
from datetime import datetime, timedelta, UTC
from github import Github
import re
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def calculate_commit_streak():
    """Calculate the current commit streak for the user."""
    try:
        # Get environment variables
        token = os.environ['GITHUB_TOKEN']
        repo_name = os.environ.get('REPO_NAME', 'BhattAyush17/BhattAyush17')
        readme_path = os.environ.get('README_PATH', 'README.md')
        username = repo_name.split('/')[0]
        
        logger.info(f"Calculating streak for user: {username}")
        logger.info(f"Repository: {repo_name}")
        
        # Initialize GitHub connection with updated authentication
        from github import Auth
        auth = Auth.Token(token)
        g = Github(auth=auth)
        repo = g.get_repo(repo_name)
        
        # Get current time using timezone-aware datetime
        now = datetime.now(UTC)
        logger.info(f"Current time (UTC): {now}")
        
        # Get commits from the last 2 years to ensure we don't miss long streaks
        since_date = now - timedelta(days=730)
        logger.info(f"Fetching commits since: {since_date}")
        
        commits = repo.get_commits(author=username, since=since_date)
        
        # Collect all commit dates
        commit_days = set()
        commit_count = 0
        for commit in commits:
            commit_date = commit.commit.author.date.date()
            commit_days.add(commit_date)
            commit_count += 1
        
        logger.info(f"Found {commit_count} commits on {len(commit_days)} unique days")
        
        # Calculate consecutive streak up to today or yesterday
        streak = 0
        day = now.date()
        
        # Check if there's a commit today, if not start from yesterday
        if day not in commit_days:
            day -= timedelta(days=1)
        
        # Count consecutive days with commits
        while day in commit_days:
            streak += 1
            day -= timedelta(days=1)
            logger.debug(f"Streak day {streak}: {day + timedelta(days=1)}")
        
        logger.info(f"Current commit streak: {streak} days")
        return streak
        
    except Exception as e:
        logger.error(f"Error calculating commit streak: {e}")
        # If we can't connect to GitHub, we should preserve the existing streak
        # rather than resetting it to 0
        logger.info("Returning 0 streak due to error - existing README will be unchanged if no update occurs")
        return 0

def update_readme_streak(streak):
    """Update the README file with the new streak count."""
    try:
        readme_path = os.environ.get('README_PATH', 'README.md')
        logger.info(f"Updating README at: {readme_path}")
        
        # Read current content
        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Create the new streak display with fire emoji
        streak_display = f"🔥 {streak}" if streak > 0 else "0"
        
        # Update streak in README between <!--STREAK_COUNT--> markers
        new_content = re.sub(
            r'<!--STREAK_COUNT-->.*?<!--STREAK_COUNT-->',
            f'<!--STREAK_COUNT-->{streak_display}<!--STREAK_COUNT-->',
            content,
            flags=re.DOTALL
        )
        
        # Check if the content actually changed
        if content == new_content:
            logger.info("No changes needed - streak is already up to date")
            return False
        
        # Write updated content
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        
        logger.info(f"Successfully updated README with streak: {streak_display}")
        return True
        
    except Exception as e:
        logger.error(f"Error updating README: {e}")
        return False

def main():
    """Main function to calculate and update commit streak."""
    try:
        logger.info("Starting streak counter update...")
        
        # Calculate current streak
        streak = calculate_commit_streak()
        
        # Update README with new streak
        updated = update_readme_streak(streak)
        
        if updated:
            logger.info("Streak counter successfully updated!")
        else:
            logger.info("No update needed")
            
        return 0
        
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
