import os
import requests
import matplotlib.pyplot as plt
from datetime import datetime
from matplotlib.patches import FancyBboxPatch

USERNAME = "BhattAyush17"
TOKEN = os.getenv("GH_STATS_TOKEN")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

GRAPHQL_URL = "https://api.github.com/graphql"

os.makedirs("assets/stats", exist_ok=True)


def query_github():
    query = """
    query($login: String!) {
      user(login: $login) {
        repositories(first: 100, ownerAffiliations: OWNER) {
          nodes {
            name
            stargazerCount
            primaryLanguage {
              name
            }
          }
        }
        contributionsCollection {
          totalCommitContributions
          totalPullRequestContributions
          totalIssueContributions
        }
        followers {
          totalCount
        }
      }
    }
    """

    response = requests.post(
        GRAPHQL_URL,
        json={"query": query, "variables": {"login": USERNAME}},
        headers=HEADERS
    )

    return response.json()


def make_stats_svg(data):
    user = data["data"]["user"]

    repos = user["repositories"]["nodes"]
    stars = sum(repo["stargazerCount"] for repo in repos)

    commits = user["contributionsCollection"]["totalCommitContributions"]
    prs = user["contributionsCollection"]["totalPullRequestContributions"]
    issues = user["contributionsCollection"]["totalIssueContributions"]
    followers = user["followers"]["totalCount"]

    fig = plt.figure(figsize=(6, 4))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")

    fig.patch.set_facecolor("#111827")

    box = FancyBboxPatch(
        (0.05, 0.05),
        0.9,
        0.9,
        boxstyle="round,pad=0.02",
        edgecolor="#6B7280",
        facecolor="#111827"
    )
    ax.add_patch(box)

    stats = [
        f"⭐ Total Stars: {stars}",
        f"💻 Total Commits: {commits}",
        f"🔀 Pull Requests: {prs}",
        f"🐛 Issues: {issues}",
        f"👥 Followers: {followers}"
    ]

    y = 0.8
    for stat in stats:
        ax.text(0.12, y, stat, fontsize=14, color="#E5E7EB")
        y -= 0.14

    plt.savefig("assets/stats/github-stats.svg", format="svg", transparent=True)
    plt.close()


def make_languages_svg(data):
    repos = data["data"]["user"]["repositories"]["nodes"]

    lang_counts = {}

    for repo in repos:
        lang = repo["primaryLanguage"]
        if lang:
            name = lang["name"]
            lang_counts[name] = lang_counts.get(name, 0) + 1

    labels = list(lang_counts.keys())[:6]
    values = list(lang_counts.values())[:6]

    fig = plt.figure(figsize=(6, 4))
    fig.patch.set_facecolor("#111827")

    plt.pie(values, labels=labels)
    plt.savefig("assets/stats/languages.svg", format="svg", transparent=True)
    plt.close()


def make_productive_svg():
    hours = [1, 2, 5, 7, 4, 8]

    fig = plt.figure(figsize=(6, 4))
    fig.patch.set_facecolor("#111827")

    plt.bar(range(len(hours)), hours)
    plt.savefig("assets/stats/productive-time.svg", format="svg", transparent=True)
    plt.close()


def make_graph_svg():
    commits = [3, 4, 2, 7, 5, 8, 6]

    fig = plt.figure(figsize=(8, 4))
    fig.patch.set_facecolor("#111827")

    plt.plot(commits)
    plt.savefig("assets/stats/contribution-graph.svg", format="svg", transparent=True)
    plt.close()


if __name__ == "__main__":
    data = query_github()
    make_stats_svg(data)
    make_languages_svg(data)
    make_productive_svg()
    make_graph_svg()
