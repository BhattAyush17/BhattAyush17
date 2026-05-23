import os
import requests
import time
import re
from bs4 import BeautifulSoup

USERNAME = "BhattAyush17"
ASSETS_DIR = "assets/stats"
BADGES_DIR = "assets/badges"

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

def update_achievements():
    """
    Scrapes user's official achievements from their profile, downloads images locally,
    and dynamically inserts them into the README between marker tags.
    """
    print("🏆 Fetching and Scraping GitHub Achievements...")
    url = f"https://github.com/{USERNAME}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            print("⚠️ Failed to fetch profile page for achievements.")
            return
            
        soup = BeautifulSoup(response.text, 'html.parser')
        badges = []
        
        # Scrape native GitHub achievements
        # Typically native achievement links contain /achievements/ in their href
        elements = soup.select('a[href*="/achievements/"]')
        for el in elements:
            img = el.find('img')
            if img:
                src = img.get('src')
                alt = img.get('alt', 'GitHub Achievement')
                if src and alt:
                    badges.append((alt, src))
                    
        if not badges:
            print("ℹ️ No achievements scraped. Keeping fallback/previous.")
            return
            
        os.makedirs(BADGES_DIR, exist_ok=True)
        
        markdown_imgs = []
        for alt, src in badges:
            # Generate a safe filename
            safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', alt.lower().replace(' ', '_'))
            ext = os.path.splitext(src.split('?')[0])[1] or '.png'
            filename = f"{safe_name}{ext}"
            filepath = os.path.join(BADGES_DIR, filename)
            
            try:
                print(f"Downloading achievement badge: {alt}...")
                img_res = requests.get(src, timeout=10)
                if img_res.status_code == 200:
                    with open(filepath, 'wb') as f:
                        f.write(img_res.content)
                    markdown_imgs.append(f'<img src="assets/badges/{filename}" width="75px" alt="{alt}" title="{alt}" />')
                else:
                    # Fallback to direct URL if download fails
                    markdown_imgs.append(f'<img src="{src}" width="75px" alt="{alt}" title="{alt}" />')
            except Exception as e:
                print(f"Error downloading {alt}: {e}")
                markdown_imgs.append(f'<img src="{src}" width="75px" alt="{alt}" title="{alt}" />')
                
        # Inject into README.md
        readme_path = "README.md"
        if os.path.exists(readme_path):
            with open(readme_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            start_marker = "<!-- START_SECTION:achievements -->"
            end_marker = "<!-- END_SECTION:achievements -->"
            
            if start_marker in content and end_marker in content:
                pattern = re.compile(rf"{start_marker}.*?{end_marker}", re.DOTALL)
                replacement = f"{start_marker}\n" + " ".join(markdown_imgs) + f"\n{end_marker}"
                new_content = pattern.sub(replacement, content)
                
                with open(readme_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                print("✅ Successfully updated achievements in README.md!")
            else:
                print("⚠️ Achievement placeholders not found in README.md.")
                
    except Exception as e:
        print(f"❌ Error updating achievements: {e}")

def main():
    print("🚀 Starting Modular & Self-Healing GitHub Stats Upgrader...")
    os.makedirs(ASSETS_DIR, exist_ok=True)
    
    # 1. Fetch SVGs
    for filename, url in URLS.items():
        fetch_and_save(filename, url)
        
    # 2. Scraping and updating native Achievements
    update_achievements()
        
    print("✨ Stats Upgrade Complete!")

if __name__ == "__main__":
    main()

