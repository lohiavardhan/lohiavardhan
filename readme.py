from datetime import datetime
import os
import time
import requests

graphql_url = "https://api.github.com/graphql"

STATS_QUERY = f"""
query userInfo($login: String!) {{
  user(login: $login) {{
    name
    login
    followers {{
      totalCount
    }}
    repositories(first: 100, ownerAffiliations: OWNER) {{
      totalCount
      nodes {{
        name
        stargazers {{
          totalCount
        }}
      }}
    }}
  }}
}}
"""

LANGUAGES_QUERY = """
query userInfo($login: String!) {
  user(login: $login) {
    repositories(ownerAffiliations: OWNER, isFork: false, first: 100) {
      nodes {
        name
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges {
            size
            node {
              color
              name
            }
          }
        }
      }
    }
  }
}
"""

REPOS_QUERY = """
query userInfo($login: String!) {
  user(login: $login) {
    repositories(first: 100, ownerAffiliations: OWNER) {
      nodes {
        nameWithOwner
      }
    }
  }
}
"""


def generate_readme(username: str, token: str, path: str = "README.md"):
    stats = get_stats(username, token)
    languages = get_languages(username, token)
    lines, commits, contributed_to = get_lines_of_code(username, token)

    with open(path, "w", encoding="utf-8") as readme:
        readme.write(f"> last updated: {datetime.now().strftime('%d %b %Y, %H:%M UTC')}\n\n")
        readme.write(f"# i'm vardhan, i write code \n\n")
        readme.write("---\n\n")

        readme.write("## 📊 stats\n")
        readme.write("```\n")
        readme.write(f"commits (all time):    {commits:,}\n")
        readme.write(f"contributed to:        {contributed_to} repos\n")
        readme.write(f"lines of code written: {lines:,}\n")
        readme.write("```\n\n")

        readme.write("## 💻 top languages\n")
        readme.write("```\n")
        total_size = sum(size for _, size in languages.items())
        for lang, size in list(languages.items())[:10]:
            percent = (size / total_size) * 100 if total_size > 0 else 0
            bar = percent_bar(percent)
            readme.write(f"{lang:<12} {bar} {percent:.2f}%\n")
        readme.write("```\n")


def get_stats(username: str, token: str):
    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }

    payload = {"query": STATS_QUERY, "variables": {"login": username}}

    response = requests.post(graphql_url, json=payload, headers=headers)
    response.raise_for_status()

    data = response.json()["data"]["user"]

    stats = {}

    stars = 0
    for repository in data["repositories"]["nodes"]:
        stars += repository["stargazers"]["totalCount"]
    stats["stars"] = stars
    stats["followers"] = data["followers"]["totalCount"]

    return stats


def get_languages(username: str, token: str):
    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }

    payload = {"query": LANGUAGES_QUERY, "variables": {"login": username}}

    response = requests.post(graphql_url, json=payload, headers=headers)
    response.raise_for_status()

    data = response.json()["data"]["user"]["repositories"]["nodes"]

    languages = {}
    for repo in data:
        edges = repo["languages"]["edges"]
        if not edges:
            continue
        for edge in edges:
            lang_name = edge["node"]["name"]
            lang_size = edge["size"]
            languages[lang_name] = languages.get(lang_name, 0) + lang_size

    # merge Jupyter Notebook into Python
    languages["Python"] = languages.get("Python", 0) + languages.pop("Jupyter Notebook", 0)

    sorted_languages = dict(
        sorted(languages.items(), key=lambda pair: pair[1], reverse=True)
    )

    return sorted_languages


def get_lines_of_code(username: str, token: str):
    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }

    payload = {"query": REPOS_QUERY, "variables": {"login": username}}
    response = requests.post(graphql_url, json=payload, headers=headers)
    response.raise_for_status()
    repos = response.json()["data"]["user"]["repositories"]["nodes"]

    total_lines = 0
    total_commits = 0
    contributed_to = 0

    for repo in repos:
        repo_name = repo["nameWithOwner"]
        stats_url = f"https://api.github.com/repos/{repo_name}/stats/contributors"

        for _ in range(3):
            r = requests.get(stats_url, headers=headers)
            if r.status_code == 200:
                contributors = r.json()
                if isinstance(contributors, list):
                    for contributor in contributors:
                        if contributor.get("author", {}).get("login", "").lower() == username.lower():
                            contributed_to += 1
                            for week in contributor.get("weeks", []):
                                total_lines += week.get("a", 0)
                                total_commits += week.get("c", 0)
                break
            elif r.status_code == 202:
                time.sleep(2)

    return total_lines, total_commits, contributed_to


def percent_bar(percent: float, width: int = 20):
    percent = max(0, min(100, percent))
    filled = round((percent / 100) * width)
    empty = width - filled
    return f"[{'█' * filled}{'░' * empty}]"


if __name__ == "__main__":
    username = "lohiavardhan"
    token = os.getenv("GITHUB_TOKEN", "")
    generate_readme(username, token)
