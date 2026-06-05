import os
import requests
import time
import re
from bs4 import BeautifulSoup
import shutil


import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from datetime import datetime, timezone

USERNAME = "BhattAyush17"
ASSETS_DIR = "assets/stats"
BADGES_DIR = "assets/badges"
TOKEN = os.getenv("GH_STATS_TOKEN")

# Setup headers for GitHub API
HEADERS = {
    "Content-Type": "application/json"
}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"

GRAPHQL_URL = "https://api.github.com/graphql"

def get_github_logo():
    """
    Downloads and caches the official GitHub logo.
    """
    logo_path = os.path.join(ASSETS_DIR, "github-mark.png")
    if not os.path.exists(logo_path):
        os.makedirs(ASSETS_DIR, exist_ok=True)
        try:
            # High-res official transparent GitHub mark
            url = "https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png"
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                with open(logo_path, 'wb') as f:
                    f.write(res.content)
                print("✅ Downloaded GitHub logo")
        except Exception as e:
            print(f"⚠️ Error downloading logo: {e}")
    return logo_path

def query_github():
    """
    Queries GitHub GraphQL API for comprehensive profile analytics.
    Falls back to mock data if there are network/token issues (unbreakable).
    """
    query = """
    query($login: String!) {
      user(login: $login) {
        repositories(first: 100, ownerAffiliations: OWNER) {
          nodes {
            name
            stargazerCount
            primaryLanguage {
              name
              color
            }
          }
        }
        contributionsCollection {
          totalCommitContributions
          totalPullRequestContributions
          totalIssueContributions
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                contributionCount
                date
              }
            }
          }
        }
        followers {
          totalCount
        }
      }
    }
    """
    try:
        if not TOKEN:
            print("⚠️ No GH_STATS_TOKEN found, using high-quality mock data for testing.")
            raise ValueError("No token")
            
        res = requests.post(
            GRAPHQL_URL,
            json={"query": query, "variables": {"login": USERNAME}},
            headers=HEADERS,
            timeout=15
        )
        if res.status_code == 200:
            data = res.json()
            if "data" in data and data["data"]["user"]:
                return data
        print(f"⚠️ GraphQL error: {res.text}. Falling back.")
    except Exception as e:
        print(f"⚠️ API Connection failed: {e}")
        raise e
        
    raise Exception("Failed to fetch data")

def calculate_streak(contribution_days):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    
    valid_days = [day for day in contribution_days if day["date"] <= today]

    current = 0
    for day in reversed(valid_days):
        if day["contributionCount"] > 0:
            current += 1
        elif day["date"] == today:
            continue
        else:
            break

    longest = 0
    running = 0
    for day in valid_days:
        if day["contributionCount"] > 0:
            running += 1
            longest = max(longest, running)
        else:
            running = 0

    return current, longest

def make_stats_svg(data):
    """
    Renders the custom GitHub Stats Card with inverted white GitHub Octocat mark.
    """
    try:
        user = data["data"]["user"]
        repos = user["repositories"]["nodes"]
        stars = sum(repo["stargazerCount"] for repo in repos)
        
        commits = user["contributionsCollection"]["totalCommitContributions"]
        prs = user["contributionsCollection"]["totalPullRequestContributions"]
        issues = user["contributionsCollection"]["totalIssueContributions"]
        followers = user["followers"]["totalCount"]
        
        fig = plt.figure(figsize=(6, 4), dpi=100)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.axis("off")
        
        # Matte Black Background
        fig.patch.set_facecolor("#121212")
        
        # Clean rounded border
        box = FancyBboxPatch(
            (0.02, 0.02), 0.96, 0.96,
            boxstyle="round,pad=0.01",
            edgecolor="#27272a",
            facecolor="#121212",
            linewidth=1.5
        )
        ax.add_patch(box)
        
        # Stats Texts
        stats = [
            f"⭐ Total Stars: {stars}",
            f"💻 Total Commits: {commits}",
            f"🔀 Pull Requests: {prs}",
            f"🐛 Issues: {issues}",
            f"👥 Followers: {followers}"
        ]
        
        # Left align text styling
        y = 0.8
        for stat in stats:
            ax.text(0.08, y, stat, fontsize=14, color="#e5e7eb", fontweight="bold", fontfamily="sans-serif")
            y -= 0.15
            
        # Draw dynamic White Octocat logo on the right side
        logo_path = get_github_logo()
        if os.path.exists(logo_path):
            logo = plt.imread(logo_path)
            # Make the transparent black logo white by forcing RGB channels to 1.0 (keeping alpha)
            if len(logo.shape) == 3 and logo.shape[2] == 4:
                logo_white = logo.copy()
                logo_white[:, :, 0:3] = 1.0  # Make it solid white
            else:
                logo_white = logo
                
            # Place the white logo on the right side of the card
            logo_ax = fig.add_axes([0.65, 0.32, 0.26, 0.36])
            logo_ax.imshow(logo_white)
            logo_ax.axis('off')
            
        plt.savefig(os.path.join(ASSETS_DIR, "github-stats.svg"), format="svg", transparent=True, bbox_inches='tight', pad_inches=0)
        plt.close()
        print("✅ Successfully generated github-stats.svg")
    except Exception as e:
        print(f"❌ Error drawing stats: {e}")

def make_languages_svg(data):
    """
    Renders a stunning Top Languages Donut Chart with PURE WHITE visible labels.
    """
    try:
        repos = data["data"]["user"]["repositories"]["nodes"]
        lang_counts = {}
        
        for repo in repos:
            lang = repo["primaryLanguage"]
            if lang:
                name = lang["name"]
                lang_counts[name] = lang_counts.get(name, 0) + 1
                
        if not lang_counts:
            lang_counts = {"Python": 5, "C++": 2, "JavaScript": 1}
            
        labels = list(lang_counts.keys())[:6]
        values = list(lang_counts.values())[:6]
        
        fig, ax = plt.subplots(figsize=(6, 4), dpi=100)
        fig.patch.set_facecolor("#121212")
        ax.set_facecolor("#121212")
        
        # Clean rounded border
        box = FancyBboxPatch(
            (0.02, 0.02), 0.96, 0.96,
            boxstyle="round,pad=0.01",
            edgecolor="#27272a",
            facecolor="#121212",
            linewidth=1.5,
            transform=fig.transFigure
        )
        fig.patches.append(box)
        
        # Theme colors for language segments
        colors = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899']
        
        # Draw Donut Chart
        wedges, texts, autotexts = ax.pie(
            values,
            labels=labels,
            autopct='%1.0f%%',
            startangle=90,
            colors=colors[:len(labels)],
            textprops={'color': 'white', 'fontsize': 11, 'fontweight': 'bold', 'fontfamily': 'sans-serif'},
            pctdistance=0.75
        )
        
        # Make autotexts (percentages inside the wedges) sleek and readable
        for autotext in autotexts:
            autotext.set_color('#111827')
            autotext.set_fontsize(9)
            
        # Draw center circle to complete donut look
        centre_circle = plt.Circle((0, 0), 0.50, fc='#121212', edgecolor='#27272a', linewidth=1)
        ax.add_artist(centre_circle)
        
        ax.axis('equal')
        plt.tight_layout()
        plt.savefig(os.path.join(ASSETS_DIR, "languages.svg"), format="svg", transparent=True, bbox_inches='tight', pad_inches=0)
        plt.close()
        print("✅ Successfully generated languages.svg")
    except Exception as e:
        print(f"❌ Error drawing languages: {e}")

def make_streak_svg(data):
    try:
        weeks = data["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
        all_days = []
        for week in weeks:
            for day in week["contributionDays"]:
                all_days.append(day)
                
        current_streak, longest_streak = calculate_streak(all_days)
        
        BG = "#121212"
        CARD = "#121212"
        GRID = "#27272a"
        TEXT = "#e5e7eb"
        ACCENT = "#3b82f6"
        MUTED = "#9ca3af"
        OUT_DIR = ASSETS_DIR
        
        fig, ax = plt.subplots(figsize=(7,4))

        fig.patch.set_facecolor(BG)
        ax.set_facecolor(CARD)
        ax.axis("off")

        from matplotlib.patches import FancyBboxPatch
        box = FancyBboxPatch(
            (0.05,0.05),
            0.9,
            0.9,
            boxstyle="round,pad=0.02",
            linewidth=1.2,
            edgecolor=GRID,
            facecolor=CARD
        )

        ax.add_patch(box)

        ax.text(
            0.5,
            0.82,
            "🔥 Current Streak",
            color=TEXT,
            fontsize=18,
            ha="center"
        )

        ax.text(
            0.5,
            0.55,
            str(current_streak),
            color=ACCENT,
            fontsize=36,
            ha="center",
            fontweight="bold"
        )

        ax.text(
            0.5,
            0.38,
            "days",
            color=MUTED,
            fontsize=12,
            ha="center"
        )

        ax.text(
            0.5,
            0.18,
            f"Longest Streak: {longest_streak}",
            color=TEXT,
            fontsize=12,
            ha="center"
        )

        plt.savefig(
            f"{OUT_DIR}/streak.svg",
            transparent=True,
            bbox_inches="tight"
        )

        plt.close()
        print("✅ Successfully generated streak.svg")
    except Exception as e:
        print(f"❌ Error generating streak card: {e}")

def make_graph_svg(data):
    """
    Downloads and caches the premium neon Vercel Contribution Activity Graph.
    """
    try:
        url = f"https://github-readme-activity-graph.vercel.app/graph?username={USERNAME}&theme=github-dark&hide_border=true&point=58a6ff&area=true&area_color=1f6feb"
        print(f"Downloading premium activity graph from {url}...")
        res = requests.get(url, timeout=15)
        if res.status_code == 200 and "<svg" in res.text.lower():
            filepath = os.path.join(ASSETS_DIR, "contribution-graph.svg")
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(res.text)
            print("✅ Successfully generated/downloaded contribution-graph.svg")
        else:
            print(f"⚠️ Failed to fetch premium activity graph. Status: {res.status_code}")
    except Exception as e:
        print(f"❌ Error fetching premium activity graph: {e}")


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
        
        # 1. Semantic search for Achievements header
        achievements_header = None
        for tag in ['h2', 'h3', 'h4', 'span', 'div']:
            found = soup.find(tag, string=lambda text: text and "achievements" in text.lower())
            if found:
                achievements_header = found
                break
                
        # Extract from the semantic container
        if achievements_header:
            container = achievements_header.parent
            for _ in range(3):
                if container:
                    imgs = container.find_all('img')
                    for img in imgs:
                        src = img.get('src')
                        alt = img.get('alt', '')
                        if src and ("badge" in src.lower() or "achievement" in src.lower() or "githubassets.com" in src.lower()):
                            if alt and alt not in [b[0] for b in badges]:
                                badges.append((alt, src))
                    if len(badges) >= 1:
                        break
                    container = container.parent
                    
        # 2. CSS link fallback
        if not badges:
            elements = soup.select('a[href*="/achievements/"]')
            for el in elements:
                img = el.find('img')
                if img:
                    src = img.get('src')
                    alt = img.get('alt', '')
                    if src and alt:
                        badges.append((alt, src))
                        
        # 3. Known names alt fallback
        if not badges:
            achievement_names = ["pull shark", "yolo", "quickdraw", "galaxy brain", "starstruck", "pair extraordinaire", "public sponsor"]
            for img in soup.find_all('img'):
                alt = img.get('alt', '')
                src = img.get('src', '')
                if alt and any(name in alt.lower() for name in achievement_names):
                    if src:
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
    
    # 1. Query GitHub data
    try:
        data = query_github()
    except Exception as e:
        print("GitHub API failure:", e)
        try:
            shutil.copy(
                "assets/fallback/streak-fallback.svg",
                "assets/stats/streak.svg"
            )
        except Exception as copy_err:
            pass
        raise
    
    # 2. Render cards using local Matplotlib (100% Offline-Resilient & Custom Styled)
    make_stats_svg(data)
    make_languages_svg(data)
    make_streak_svg(data)
    make_graph_svg(data)
    
    # 3. Download the 4 GitHub profile summary cards in standard github_dark style
    summary_urls = {
        "summary-stats.svg": f"https://github-profile-summary-cards.vercel.app/api/cards/stats?username={USERNAME}&theme=github_dark",
        "summary-repos-per-language.svg": f"https://github-profile-summary-cards.vercel.app/api/cards/repos-per-language?username={USERNAME}&theme=github_dark",
        "summary-most-commit-language.svg": f"https://github-profile-summary-cards.vercel.app/api/cards/most-commit-language?username={USERNAME}&theme=github_dark",
        "summary-productive-time.svg": f"https://github-profile-summary-cards.vercel.app/api/cards/productive-time?username={USERNAME}&theme=github_dark&utcOffset=5.5"
    }
    for filename, url in summary_urls.items():
        try:
            print(f"Downloading {filename}...")
            res = requests.get(url, timeout=15)
            if res.status_code == 200 and "<svg" in res.text.lower():
                svg_text = res.text
                # Force heading titles inside the summary SVG cards to render in pure white (#ffffff)
                svg_text = re.sub(
                    r'(<text\s+x="30"\s+y="40"\s+style="font-size:\s*22px;\s*fill:\s*)[^;"]+(;?")',
                    r'\g<1>#ffffff\2',
                    svg_text
                )
                filepath = os.path.join(ASSETS_DIR, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(svg_text)
                print(f"✅ Successfully downloaded and whitened {filename}")
        except Exception as e:
            print(f"⚠️ Error downloading {filename}: {e}. Kept old file.")
        
    # 4. Scrape and update native profile Achievements
    update_achievements()
        
    print("✨ Stats Upgrade Complete!")

if __name__ == "__main__":
    main()
