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
    lines = get_lines_of_code(username, token)
    commits = get_all_time_commits(username, token)
    contributed_to = get_all_time_contributed_to(username, token)


    with open(path, "w", encoding="utf-8") as readme:
        readme.write(f"> last updated: {datetime.now().strftime('%d %b %Y, %H:%M UTC')}\n\n")
        readme.write(f"# i'm vardhan, i write code \n\n")
        readme.write("---\n\n")

        readme.write("## 📊 stats\n")
        readme.write("```\n")
        readme.write(f"commits:               {commits:,}\n")
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
                            for week in contributor.get("weeks", []):
                                total_lines += week.get("a", 0)
                break
            elif r.status_code == 202:
                time.sleep(2)

    return total_lines

def get_all_time_commits(username: str, token: str):
    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }

    # First get the year the user joined
    joined_query = """
    query userInfo($login: String!) {
      user(login: $login) {
        createdAt
      }
    }
    """
    response = requests.post(graphql_url, json={"query": joined_query, "variables": {"login": username}}, headers=headers)
    response.raise_for_status()
    created_at = response.json()["data"]["user"]["createdAt"]
    join_year = int(created_at[:4])

    # Query each year from join year to now
    total_commits = 0
    for year in range(join_year, datetime.now().year + 1):
        from_date = f"{year}-01-01T00:00:00Z"
        to_date = f"{year}-12-31T23:59:59Z"
        query = f"""
        query userInfo($login: String!) {{
          user(login: $login) {{
            contributionsCollection(from: "{from_date}", to: "{to_date}") {{
              totalCommitContributions
              restrictedContributionsCount
            }}
          }}
        }}
        """
        r = requests.post(graphql_url, json={"query": query, "variables": {"login": username}}, headers=headers)
        r.raise_for_status()
        data = r.json()["data"]["user"]["contributionsCollection"]
        # totalCommitContributions = public, restrictedContributionsCount = private
        total_commits += data["totalCommitContributions"] + data["restrictedContributionsCount"]

    return total_commits

def get_all_time_contributed_to(username: str, token: str):
    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }

    joined_query = """
    query userInfo($login: String!) {
      user(login: $login) {
        createdAt
      }
    }
    """
    response = requests.post(graphql_url, json={"query": joined_query, "variables": {"login": username}}, headers=headers)
    response.raise_for_status()
    join_year = int(response.json()["data"]["user"]["createdAt"][:4])

    repos = set()
    for year in range(join_year, datetime.now().year + 1):
        from_date = f"{year}-01-01T00:00:00Z"
        to_date = f"{year}-12-31T23:59:59Z"
        query = f"""
        query userInfo($login: String!) {{
          user(login: $login) {{
            contributionsCollection(from: "{from_date}", to: "{to_date}") {{
              commitContributionsByRepository {{
                repository {{
                  nameWithOwner
                }}
              }}
            }}
          }}
        }}
        """
        r = requests.post(graphql_url, json={"query": query, "variables": {"login": username}}, headers=headers)
        r.raise_for_status()
        contribs = r.json()["data"]["user"]["contributionsCollection"]["commitContributionsByRepository"]
        for contrib in contribs:
            repos.add(contrib["repository"]["nameWithOwner"])

    return len(repos)

def percent_bar(percent: float, width: int = 20):
    percent = max(0, min(100, percent))
    filled = round((percent / 100) * width)
    empty = width - filled
    return f"[{'█' * filled}{'░' * empty}]"


if __name__ == "__main__":
    username = "lohiavardhan"
    token = os.getenv("GITHUB_TOKEN", "")
    generate_readme(username, token)
