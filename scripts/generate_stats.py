import os
import requests
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from bs4 import BeautifulSoup

USERNAME = "BhattAyush17"
TOKEN = os.getenv("GH_STATS_TOKEN")

if not TOKEN:
    raise RuntimeError("GH_STATS_TOKEN missing")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "GitHub Analytics Generator"
}

OUT_DIR = "assets/stats"
os.makedirs(OUT_DIR, exist_ok=True)

# Theme
BG = "#0B0F19"
CARD = "#111827"
TEXT = "#D1D5DB"
MUTED = "#9CA3AF"
ACCENT = "#E5E7EB"
GRID = "#1F2937"
BAR = "#9CA3AF"


# ==========================================
# HELPERS
# ==========================================
def github_graphql(query):
    r = requests.post(
        "https://api.github.com/graphql",
        json={"query": query},
        headers=HEADERS,
        timeout=30
    )

    if r.status_code != 200:
        raise RuntimeError(f"GitHub API error: {r.status_code}")

    payload = r.json()

    if "errors" in payload:
        raise RuntimeError(payload["errors"])

    return payload["data"]


def fetch_achievements(username):
    url = f"https://github.com/{username}"

    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20
    )

    soup = BeautifulSoup(r.text, "html.parser")

    achievements = []

    for img in soup.find_all("img"):
        alt = img.get("alt", "")

        if "achievement" in alt.lower():
            cleaned = alt.replace("Achievement:", "").strip()

            if cleaned and cleaned not in achievements:
                achievements.append(cleaned)

    return achievements


# ==========================================
# FETCH DATA
# ==========================================
query = f"""
query {{
  user(login: "{USERNAME}") {{
    followers {{
      totalCount
    }}

    repositories(
      ownerAffiliations: OWNER,
      isFork: false,
      first: 100
    ) {{
      totalCount
      nodes {{
        stargazerCount
        languages(
          first: 10,
          orderBy: {{
            field: SIZE,
            direction: DESC
          }}
        ) {{
          edges {{
            size
            node {{
              name
            }}
          }}
        }}
      }}
    }}

    contributionsCollection {{
      contributionCalendar {{
        totalContributions
        weeks {{
          contributionDays {{
            date
            contributionCount
          }}
        }}
      }}

      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
    }}
  }}
}}
"""

data = github_graphql(query)["user"]

followers = data["followers"]["totalCount"]
repos = data["repositories"]["totalCount"]
commits = data["contributionsCollection"]["totalCommitContributions"]
prs = data["contributionsCollection"]["totalPullRequestContributions"]
issues = data["contributionsCollection"]["totalIssueContributions"]

stars = sum(
    repo["stargazerCount"]
    for repo in data["repositories"]["nodes"]
)

# ==========================================
# LANGUAGES
# ==========================================
lang_sizes = {}

for repo in data["repositories"]["nodes"]:
    for edge in repo["languages"]["edges"]:
        lang = edge["node"]["name"]
        size = edge["size"]
        lang_sizes[lang] = lang_sizes.get(lang, 0) + size

top_langs = sorted(
    lang_sizes.items(),
    key=lambda x: x[1],
    reverse=True
)[:6]

lang_labels = [x[0] for x in top_langs]
lang_sizes_only = [x[1] for x in top_langs]
total_lang_size = sum(lang_sizes_only)

# ==========================================
# CONTRIBUTION DATA
# ==========================================
days = []
counts = []

weeks = data["contributionsCollection"]["contributionCalendar"]["weeks"]

for week in weeks:
    for day in week["contributionDays"]:
        days.append(day["date"][5:])  # MM-DD
        counts.append(day["contributionCount"])

recent_days = days[-30:]
recent_counts = counts[-30:]

# ==========================================
# PRODUCTIVITY APPROXIMATION
# ==========================================
hour_slots = list(range(24))
hour_commits = [0] * 24

for i, c in enumerate(recent_counts):
    hour_commits[(i * 3) % 24] += c

# ==========================================
# STATS CARD
# ==========================================
fig, ax = plt.subplots(figsize=(7, 4))
fig.patch.set_facecolor(BG)
ax.set_facecolor(CARD)
ax.axis("off")

card = patches.FancyBboxPatch(
    (0.03, 0.05),
    0.94,
    0.9,
    boxstyle="round,pad=0.02",
    linewidth=1.2,
    edgecolor=GRID,
    facecolor=CARD
)
ax.add_patch(card)

stats = [
    ("★ Total Stars", stars),
    ("⌁ Total Commits", commits),
    ("⇅ Pull Requests", prs),
    ("⚠ Issues", issues),
    ("👥 Followers", followers),
    ("📦 Repositories", repos),
]

y = 0.84
for label, value in stats:
    ax.text(
        0.12,
        y,
        label,
        color=TEXT,
        fontsize=13,
        ha="left"
    )

    ax.text(
        0.88,
        y,
        str(value),
        color=ACCENT,
        fontsize=14,
        ha="right",
        fontweight="bold"
    )

    y -= 0.12

plt.savefig(
    f"{OUT_DIR}/github-stats.svg",
    transparent=True,
    bbox_inches="tight"
)
plt.close()

# ==========================================
# LANGUAGE CARD (PROGRESS BARS)
# ==========================================
fig, ax = plt.subplots(figsize=(7, 4))
fig.patch.set_facecolor(BG)
ax.set_facecolor(CARD)

ax.set_xlim(0, 100)
ax.set_ylim(0, len(lang_labels))
ax.axis("off")

for i, (lang, size) in enumerate(top_langs):
    pct = round(size / total_lang_size * 100, 1)
    y = len(lang_labels) - i - 1

    ax.barh(y, 100, color=GRID, height=0.5)
    ax.barh(y, pct, color=BAR, height=0.5)

    ax.text(0, y + 0.28, lang, color=TEXT, fontsize=11)
    ax.text(101, y, f"{pct}%", color=ACCENT, fontsize=10)

ax.set_title(
    "Top Languages by Repository",
    color=ACCENT,
    fontsize=15,
    pad=20
)

plt.savefig(
    f"{OUT_DIR}/languages.svg",
    transparent=True,
    bbox_inches="tight"
)
plt.close()

# ==========================================
# CONTRIBUTION GRAPH
# ==========================================
fig, ax = plt.subplots(figsize=(14, 5))
fig.patch.set_facecolor(BG)
ax.set_facecolor(CARD)

ax.plot(
    range(len(recent_counts)),
    recent_counts,
    color=ACCENT,
    linewidth=2,
    marker="o",
    markersize=4
)

for i, v in enumerate(recent_counts):
    if v > 0:
        ax.text(
            i,
            v + 0.2,
            str(v),
            color=TEXT,
            fontsize=7,
            ha="center"
        )

tick_idx = list(range(0, len(recent_days), 4))

ax.set_xticks(tick_idx)
ax.set_xticklabels(
    [recent_days[i] for i in tick_idx],
    rotation=45,
    color=MUTED
)

ax.set_ylabel("Commits", color=TEXT)
ax.set_title(
    f"{USERNAME}'s Contribution Graph | Total: {sum(recent_counts)}",
    color=ACCENT,
    fontsize=15
)

ax.grid(True, color=GRID, linestyle="--", alpha=0.5)
ax.tick_params(axis="y", colors=MUTED)

plt.savefig(
    f"{OUT_DIR}/contribution-graph.svg",
    transparent=True,
    bbox_inches="tight"
)
plt.close()

# ==========================================
# PRODUCTIVITY
# ==========================================
fig, ax = plt.subplots(figsize=(14, 4))
fig.patch.set_facecolor(BG)
ax.set_facecolor(CARD)

ax.bar(hour_slots, hour_commits, color=BAR)

ax.set_title(
    "Productive Time (Commit Activity Approximation)",
    color=ACCENT,
    fontsize=15
)

ax.set_xlabel("Hour of Day", color=TEXT)
ax.set_ylabel("Commits", color=TEXT)

ax.tick_params(colors=MUTED)
ax.grid(axis="y", color=GRID)

plt.savefig(
    f"{OUT_DIR}/productive-time.svg",
    transparent=True,
    bbox_inches="tight"
)
plt.close()

# ==========================================
# ACHIEVEMENTS
# ==========================================
achievements = fetch_achievements(USERNAME)

fig, ax = plt.subplots(figsize=(14, 2.2))
fig.patch.set_facecolor(BG)
ax.set_facecolor(CARD)
ax.axis("off")

box = patches.FancyBboxPatch(
    (0.01, 0.05),
    0.98,
    0.9,
    boxstyle="round,pad=0.02",
    linewidth=1.2,
    edgecolor=GRID,
    facecolor=CARD
)
ax.add_patch(box)

x = 0.04

for badge in achievements:
    width = min(0.18, 0.04 + len(badge) * 0.008)

    pill = patches.FancyBboxPatch(
        (x, 0.35),
        width,
        0.3,
        boxstyle="round,pad=0.02",
        linewidth=1,
        edgecolor="#6B7280",
        facecolor="#374151"
    )
    ax.add_patch(pill)

    ax.text(
        x + width / 2,
        0.5,
        badge,
        ha="center",
        va="center",
        fontsize=9,
        color=TEXT
    )

    x += width + 0.02

    if x > 0.9:
        break

plt.savefig(
    f"{OUT_DIR}/achievements.svg",
    transparent=True,
    bbox_inches="tight"
)
plt.close()
