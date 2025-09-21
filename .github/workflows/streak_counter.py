import os
from datetime import datetime, timedelta
from github import Github
import re

token = os.environ['GITHUB_TOKEN']
repo_name = os.environ.get('REPO_NAME', 'BhattAyush17/BhattAyush17')
readme_path = os.environ.get('README_PATH', 'README.md')
username = repo_name.split('/')[0]

g = Github(token)
repo = g.get_repo(repo_name)

now = datetime.utcnow()
commits = repo.get_commits(author=username, since=now - timedelta(days=365))

commit_days = set()
for commit in commits:
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
