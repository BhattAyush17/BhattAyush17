import os
import requests
import time

USERNAME = "BhattAyush17"
ASSETS_DIR = "assets/stats"

# List of assets to fetch and their URLs (Enforcing custom Premium Matte Black, White, and Silver Monochrome Palette)
URLS = {
    # 1. GitHub Stats (Row of Three)
    "github-stats.svg": f"https://github-readme-stats.vercel.app/api?username={USERNAME}&show_icons=true&bg_color=121212&title_color=ffffff&text_color=e5e7eb&icon_color=a1a1aa&hide_border=true",
    "streak.svg": f"https://streak-stats.demolab.com?user={USERNAME}&theme=dark&hide_border=true&background=121212&fire=ffffff&ring=ffffff&stroke=a1a1aa&currStreakNum=ffffff&sideNums=e5e7eb&currStreakLabel=a1a1aa&sideLabels=9ca3af",
    "languages.svg": f"https://github-readme-stats.vercel.app/api/top-langs/?username={USERNAME}&layout=compact&bg_color=121212&title_color=ffffff&text_color=e5e7eb&icon_color=a1a1aa&hide_border=true",
    
    # 2. Activity / Contribution Graph
    "contribution-graph.svg": f"https://github-readme-activity-graph.vercel.app/graph?username={USERNAME}&theme=github-dark&hide_border=true&bg_color=121212&color=ffffff&line=a1a1aa&point=ffffff&area=true",
    
    # 3. GitHub Profile Summary Cards (2x2 Grid)
    "summary-stats.svg": f"https://github-profile-summary-cards.vercel.app/api/cards/stats?username={USERNAME}&theme=github_dark",
    "summary-repos-per-language.svg": f"https://github-profile-summary-cards.vercel.app/api/cards/repos-per-language?username={USERNAME}&theme=github_dark",
    "summary-most-commit-language.svg": f"https://github-profile-summary-cards.vercel.app/api/cards/most-commit-language?username={USERNAME}&theme=github_dark",
    "summary-productive-time.svg": f"https://github-profile-summary-cards.vercel.app/api/cards/productive-time?username={USERNAME}&theme=github_dark&utcOffset=5.5"
}

def fetch_and_save(filename, url, retries=3):
    """
    Safely fetches SVG from url and saves it. 
    If it fails, it keeps the existing SVG (unbreakable self-healing).
    """
    filepath = os.path.join(ASSETS_DIR, filename)
    
    for attempt in range(retries):
        try:
            print(f"Fetching {filename} (Attempt {attempt+1}/{retries})...")
            response = requests.get(url, timeout=15)
            
            # Check if successful and seems like a valid SVG
            if response.status_code == 200 and "<svg" in response.text.lower():
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(response.text)
                print(f"✅ Successfully updated {filename}")
                return True
            else:
                print(f"⚠️ Failed to fetch {filename}. Status code: {response.status_code}")
                
        except Exception as e:
            print(f"❌ Error fetching {filename}: {e}")
            
        time.sleep(2)
        
    print(f"⏭️ Skipping {filename}. Kept existing file for self-healing.")
    return False

def main():
    print("🚀 Starting Modular & Self-Healing GitHub Stats Upgrader...")
    os.makedirs(ASSETS_DIR, exist_ok=True)
    
    for filename, url in URLS.items():
        fetch_and_save(filename, url)
        
    print("✨ Stats Upgrade Complete!")

if __name__ == "__main__":
    main()
