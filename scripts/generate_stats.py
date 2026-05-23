import os
import requests
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import matplotlib.patches as patches

USERNAME = "BhattAyush17"
TOKEN = os.getenv("GH_STATS_TOKEN")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

OUT_DIR = "assets/stats"
os.makedirs(OUT_DIR, exist_ok=True)

BG = "#0B0F19"
CARD = "#111827"
TEXT = "#D1D5DB"
MUTED = "#9CA3AF"
ACCENT = "#E5E7EB"
GRID = "#1F2937"


# ---------------------------
# FETCH GITHUB DATA
# ---------------------------
query = """
query {
  user(login: "BhattAyush17") {
    followers {
      totalCount
    }
    repositories(ownerAffiliations: OWNER, isFork: false, first: 100) {
      totalCount
      nodes {
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges {
            size
            node {
              name
            }
          }
        }
      }
    }
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            date
            contributionCount
          }
        }
      }
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
    }
  }
}
"""

resp = requests.post(
    "https://api.github.com/graphql",
    json={"query": query},
    headers=HEADERS
)

data = resp.json()["data"]["user"]

followers = data["followers"]["totalCount"]
repos = data["repositories"]["totalCount"]
commits = data["contributionsCollection"]["totalCommitContributions"]
prs = data["contributionsCollection"]["totalPullRequestContributions"]
issues = data["contributionsCollection"]["totalIssueContributions"]

stars = sum(repo["stargazerCount"] for repo in data["repositories"]["nodes"])

# ---------------------------
# LANGUAGE AGGREGATION
# ---------------------------
lang_sizes = {}

for repo in data["repositories"]["nodes"]:
    for edge in repo["languages"]["edges"]:
        lang = edge["node"]["name"]
        size = edge["size"]
        lang_sizes[lang] = lang_sizes.get(lang, 0) + size

top_langs = sorted(lang_sizes.items(), key=lambda x: x[1], reverse=True)[:6]
labels = [x[0] for x in top_langs]
sizes = [x[1] for x in top_langs]

# ---------------------------
# CONTRIBUTION DATA
# ---------------------------
days = []
counts = []

weeks = data["contributionsCollection"]["contributionCalendar"]["weeks"]

for week in weeks:
    for day in week["contributionDays"]:
        days.append(day["date"])
        counts.append(day["contributionCount"])

recent_days = days[-30:]
recent_counts = counts[-30:]

# ---------------------------
# PRODUCTIVITY DATA
# (approximation until hourly API added)
# ---------------------------
hour_slots = [0, 2, 5, 7, 9, 11, 14, 16, 20, 22]
hour_commits = [1, 2, 5, 8, 7, 4, 6, 3, 8, 5]


# ---------------------------
# STATS CARD
# ---------------------------
fig, ax = plt.subplots(figsize=(7, 4))
fig.patch.set_facecolor(BG)
ax.set_facecolor(CARD)
ax.axis("off")

box = patches.FancyBboxPatch(
    (0.03, 0.05),
    0.94,
    0.9,
    boxstyle="round,pad=0.02",
    linewidth=1.5,
    edgecolor=GRID,
    facecolor=CARD
)
ax.add_patch(box)

stats = [
    ("★ Total Stars", stars),
    ("⌁ Total Commits", commits),
    ("⇅ Pull Requests", prs),
    ("⚠ Issues", issues),
    ("👥 Followers", followers),
    ("📦 Repositories", repos),
]

y = 0.82
for label, value in stats:
    ax.text(0.12, y, label, color=TEXT, fontsize=13, ha="left")
    ax.text(0.88, y, str(value), color=ACCENT, fontsize=14, ha="right", fontweight="bold")
    y -= 0.12

plt.savefig(f"{OUT_DIR}/github-stats.svg", transparent=True, bbox_inches="tight")
plt.close()


# ---------------------------
# LANGUAGES
# ---------------------------
fig, ax = plt.subplots(figsize=(7, 4))
fig.patch.set_facecolor(BG)
ax.set_facecolor(CARD)

colors = ["#E5E7EB", "#D1D5DB", "#9CA3AF", "#6B7280", "#4B5563", "#374151"]

wedges, _ = ax.pie(
    sizes,
    startangle=90,
    colors=colors,
    wedgeprops=dict(width=0.4, edgecolor=BG)
)

ax.legend(
    wedges,
    [f"{l} ({round(s/sum(sizes)*100,1)}%)" for l, s in zip(labels, sizes)],
    loc="center left",
    bbox_to_anchor=(1, 0.5),
    frameon=False,
    labelcolor=TEXT
)

ax.set_title("Top Languages by Repository", color=ACCENT, fontsize=15, pad=20)

plt.savefig(f"{OUT_DIR}/languages.svg", transparent=True, bbox_inches="tight")
plt.close()


# ---------------------------
# CONTRIBUTION GRAPH
# ---------------------------
fig, ax = plt.subplots(figsize=(14, 5))
fig.patch.set_facecolor(BG)
ax.set_facecolor(CARD)

ax.plot(
    recent_days,
    recent_counts,
    color=ACCENT,
    linewidth=2,
    marker="o",
    markersize=5
)

for i, val in enumerate(recent_counts):
    if val > 0:
        ax.text(i, val + 0.3, str(val), color=TEXT, fontsize=8, ha="center")

ax.set_title(
    f"{USERNAME}'s Contribution Graph\nTotal Contributions: {sum(recent_counts)}",
    color=ACCENT,
    fontsize=16
)

ax.set_ylabel("Commits", color=TEXT)
ax.tick_params(colors=MUTED)
ax.grid(True, color=GRID, linestyle="--", alpha=0.5)

plt.xticks(rotation=45)

plt.savefig(f"{OUT_DIR}/contribution-graph.svg", transparent=True, bbox_inches="tight")
plt.close()


# ---------------------------
# PRODUCTIVITY
# ---------------------------
fig, ax = plt.subplots(figsize=(14, 4))
fig.patch.set_facecolor(BG)
ax.set_facecolor(CARD)

ax.bar(hour_slots, hour_commits, color="#9CA3AF")

ax.set_title("Productivity (Commits by Hour)", color=ACCENT, fontsize=16)
ax.set_xlabel("Hour of Day", color=TEXT)
ax.set_ylabel("Commits", color=TEXT)
ax.tick_params(colors=MUTED)
ax.grid(axis="y", color=GRID)

plt.savefig(f"{OUT_DIR}/productive-time.svg", transparent=True, bbox_inches="tight")
plt.close()
