import os
from datetime import datetime, timedelta
from github import Github
import re

token = os.environ['GITHUB_TOKEN']
repo_name = os.environ.get('REPO_NAME', 'BhattAyush17/BhattAyush17')
readme_path = os.environ.get('README_PATH', 'README.md')
branch = os.environ.get('BRANCH', 'main')  # Default branch

g = Github(token)
repo = g.get_repo(repo_name)

now = datetime.utcnow()
since = now - timedelta(days=365)
commits = repo.get_commits(since=since, sha=branch)

commit_days = set()
for commit in commits:
    # If you want to filter only your commits, uncomment the next line:
    # if commit.author and commit.author.login != "BhattAyush17": continue
    commit_date = commit.commit.author.date.date()
    commit_days.add(commit_date)

# Calculate consecutive streak up to today
streak = 0
day = now.date()
while day in commit_days:
    streak += 1
    day -= timedelta(days=1)

# Update streak in README between <!--STREAK_COUNT--> markers
with open(readme_path, "r", encoding="utf-8") as f:
    content = f.read()

new_content = re.sub(
    r'<!--STREAK_COUNT-->.*?<!--STREAK_COUNT-->',
    f'<!--STREAK_COUNT-->{streak}<!--STREAK_COUNT-->',
    content,
    flags=re.DOTALL
)

with open(readme_path, "w", encoding="utf-8") as f:
    f.write(new_content)
