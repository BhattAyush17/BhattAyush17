#!/usr/bin/env python3
"""
Test the streak counter functionality with mock data
"""
import sys
import os
sys.path.insert(0, '.github/scripts')

from update_streak import calculate_streak, update_readme
from datetime import datetime, timedelta, timezone

def test_streak_calculation():
    """Test streak calculation with mock events"""
    print("🧪 Testing streak calculation...")
    
    # Mock events for testing
    today = datetime.now(timezone.utc)
    mock_events = []
    
    # Create events for the last 5 days
    for i in range(5):
        event_date = (today - timedelta(days=i)).isoformat()
        mock_events.append({
            'type': 'PushEvent',
            'created_at': event_date
        })
    
    # Calculate streak
    streak = calculate_streak(mock_events)
    print(f"✅ Calculated streak with mock data: {streak} days")
    
    # Test with no events
    empty_streak = calculate_streak([])
    print(f"✅ Calculated streak with no events: {empty_streak} days")
    
    return streak > 0

def test_readme_update():
    """Test README update functionality"""
    print("🧪 Testing README update...")
    
    # Make a backup
    os.system('cp README.md README.md.backup')
    
    # Test updating with a test value
    success = update_readme(42)
    
    if success:
        # Check if the value was updated
        with open('README.md', 'r') as f:
            content = f.read()
            if '<!--STREAK_COUNT-->42<!--STREAK_COUNT-->' in content:
                print("✅ README update test successful!")
                result = True
            else:
                print("❌ README update test failed - value not found")
                result = False
    else:
        print("❌ README update returned false")
        result = False
    
    # Restore backup
    os.system('mv README.md.backup README.md')
    
    return result

def main():
    """Run all tests"""
    print("🔥 Testing GitHub Streak Counter")
    
    test1 = test_streak_calculation()
    test2 = test_readme_update()
    
    if test1 and test2:
        print("✨ All tests passed!")
        return 0
    else:
        print("❌ Some tests failed!")
        return 1

if __name__ == "__main__":
    sys.exit(main())