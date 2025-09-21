import os
import subprocess
from datetime import datetime, timedelta
import re

# Setup
readme_path = os.environ.get('README_PATH', 'README.md')

# Get commit history using git log
def get_commit_dates():
    """Get commit dates from git history"""
    try:
        # Get commits from last 365 days with author email and date
        result = subprocess.run([
            'git', 'log', 
            '--since=365 days ago',
            '--pretty=format:%ad',
            '--date=short'
        ], capture_output=True, text=True, check=True)
        
        commit_dates = []
        for line in result.stdout.strip().split('\n'):
            if line:
                try:
                    date_obj = datetime.strptime(line, '%Y-%m-%d').date()
                    commit_dates.append(date_obj)
                except ValueError:
                    continue
        
        return set(commit_dates)
    except subprocess.CalledProcessError:
        print("Error getting git history")
        return set()

# Get commit dates
commit_days = get_commit_dates()
print(f"Found commits on {len(commit_days)} different days")

# Calculate streak: consecutive days with commits up to today
streak = 0
now = datetime.now()
day = now.date()

# Check if there are commits today or yesterday (to be more lenient)
if day not in commit_days and (day - timedelta(days=1)) in commit_days:
    day = day - timedelta(days=1)

while day in commit_days:
    streak += 1
    day -= timedelta(days=1)
    # Prevent infinite loop
    if streak > 365:
        break

print(f"Calculated streak: {streak} days")

# Update README between <!--STREAK_COUNT--> markers
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

print(f"Updated README.md with streak count: {streak}")

# Update README between <!--STREAK_COUNT--> markers
with open(readme_path, "r", encoding="utf-8") as f:
    content = f.read()

import re
new_content = re.sub(
    r'<!--STREAK_COUNT-->.*?<!--STREAK_COUNT-->',
    f'<!--STREAK_COUNT-->{streak}<!--STREAK_COUNT-->',
    content,
    flags=re.DOTALL
)

with open(readme_path, "w", encoding="utf-8") as f:
    f.write(new_content)
