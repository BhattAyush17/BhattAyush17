#!/usr/bin/env python3
"""
GitHub Streak Counter - Calculates and updates the daily commit streak
"""
import os
import requests
import re
from datetime import datetime, timedelta, timezone
from dateutil import parser
import json

# Configuration
USERNAME = os.environ.get('USERNAME', 'BhattAyush17')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
README_FILE = 'README.md'
STREAK_START_MARKER = '<!--STREAK_COUNT-->'
STREAK_END_MARKER = '<!--STREAK_COUNT-->'

def get_user_events(username, token, days_back=365):
    """Fetch user's public events from GitHub API"""
    headers = {
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'streak-counter-bot'
    }
    if token:
        headers['Authorization'] = f'token {token}'
    
    events = []
    page = 1
    per_page = 100
    
    print(f"Fetching events for user: {username}")
    
    while len(events) < days_back * 10 and page <= 10:  # Limit to prevent excessive API calls
        url = f'https://api.github.com/users/{username}/events?page={page}&per_page={per_page}'
        
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            
            page_events = response.json()
            if not page_events:
                break
                
            events.extend(page_events)
            page += 1
            
            # Check rate limiting
            remaining = int(response.headers.get('X-RateLimit-Remaining', 0))
            if remaining < 10:
                print(f"Rate limit warning: {remaining} requests remaining")
                break
                
        except requests.exceptions.RequestException as e:
            print(f"Error fetching events: {e}")
            break
    
    print(f"Fetched {len(events)} events")
    return events

def calculate_streak(events):
    """Calculate current consecutive day streak from events"""
    if not events:
        return 0
    
    # Filter for commit-like events
    commit_events = [
        'PushEvent', 'CreateEvent', 'DeleteEvent', 
        'PullRequestEvent', 'IssuesEvent', 'CommitCommentEvent'
    ]
    
    # Get dates of activity
    activity_dates = set()
    for event in events:
        if event.get('type') in commit_events:
            try:
                event_date = parser.parse(event['created_at']).date()
                activity_dates.add(event_date)
            except (ValueError, KeyError):
                continue
    
    if not activity_dates:
        return 0
    
    # Sort dates in descending order
    sorted_dates = sorted(activity_dates, reverse=True)
    
    # Calculate streak from today backwards
    today = datetime.now(timezone.utc).date()
    current_streak = 0
    
    # Check if there's activity today or yesterday (account for timezone differences)
    check_date = today
    found_recent_activity = False
    
    # Allow up to 2 days back to account for timezone differences
    for i in range(2):
        if check_date in sorted_dates:
            found_recent_activity = True
            break
        check_date = today - timedelta(days=i+1)
    
    if not found_recent_activity:
        return 0
    
    # Start counting from the most recent activity date
    streak_date = max(date for date in sorted_dates if date <= today)
    
    # Count consecutive days backwards
    while streak_date in sorted_dates:
        current_streak += 1
        streak_date -= timedelta(days=1)
    
    return current_streak

def update_readme(streak_count):
    """Update README.md with the new streak count"""
    try:
        with open(README_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Pattern to match the streak count between markers
        pattern = f'{re.escape(STREAK_START_MARKER)}.*?{re.escape(STREAK_END_MARKER)}'
        replacement = f'{STREAK_START_MARKER}{streak_count}{STREAK_END_MARKER}'
        
        updated_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
        
        if updated_content != content:
            with open(README_FILE, 'w', encoding='utf-8') as f:
                f.write(updated_content)
            print(f"✅ Updated README.md with streak count: {streak_count}")
            return True
        else:
            print(f"ℹ️ No changes needed. Streak count is already: {streak_count}")
            return False
            
    except FileNotFoundError:
        print(f"❌ Error: {README_FILE} not found")
        return False
    except Exception as e:
        print(f"❌ Error updating README: {e}")
        return False

def main():
    """Main function"""
    print("🔥 GitHub Streak Counter")
    print(f"Target user: {USERNAME}")
    
    # Fetch user events
    events = get_user_events(USERNAME, GITHUB_TOKEN)
    
    # Calculate streak
    streak_count = calculate_streak(events)
    print(f"📊 Current streak: {streak_count} days")
    
    # Update README
    updated = update_readme(streak_count)
    
    if updated:
        print("✨ Streak counter updated successfully!")
    else:
        print("📝 No updates made to README.md")
    
    return streak_count

if __name__ == "__main__":
    main()