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


def make_request(method: str, url: str, headers: dict, retries: int = 5, backoff: float = 2.0, **kwargs):
    """Make an HTTP request with exponential backoff retry on 5xx errors."""
    for attempt in range(retries):
        if method == "post":
            response = requests.post(url, headers=headers, **kwargs)
        else:
            response = requests.get(url, headers=headers, **kwargs)

        if response.status_code < 500:
            response.raise_for_status()
            return response

        wait = backoff * (2 ** attempt)
        print(f"  [{response.status_code}] Retrying in {wait:.0f}s (attempt {attempt + 1}/{retries})...")
        time.sleep(wait)

    # Final attempt — let it raise naturally
    response.raise_for_status()
    return response


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
    response = make_request("post", graphql_url, headers=headers, json=payload)
    data = response.json()["data"]["user"]

    stars = sum(repo["stargazers"]["totalCount"] for repo in data["repositories"]["nodes"])
    return {"stars": stars, "followers": data["followers"]["totalCount"]}


def get_languages(username: str, token: str):
    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }

    payload = {"query": LANGUAGES_QUERY, "variables": {"login": username}}
    response = make_request("post", graphql_url, headers=headers, json=payload)
    data = response.json()["data"]["user"]["repositories"]["nodes"]

    languages = {}
    for repo in data:
        for edge in repo["languages"]["edges"]:
            lang_name = edge["node"]["name"]
            lang_size = edge["size"]
            languages[lang_name] = languages.get(lang_name, 0) + lang_size

    # merge Jupyter Notebook into Python
    languages["Python"] = languages.get("Python", 0) + languages.pop("Jupyter Notebook", 0)

    return dict(sorted(languages.items(), key=lambda pair: pair[1], reverse=True))


def get_lines_of_code(username: str, token: str):
    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }

    payload = {"query": REPOS_QUERY, "variables": {"login": username}}
    response = make_request("post", graphql_url, headers=headers, json=payload)
    repos = response.json()["data"]["user"]["repositories"]["nodes"]

    total_lines = 0

    for repo in repos:
        repo_name = repo["nameWithOwner"]
        stats_url = f"https://api.github.com/repos/{repo_name}/stats/contributors"

        for attempt in range(6):
            r = make_request("get", stats_url, headers=headers)
            if r.status_code == 200:
                contributors = r.json()
                if isinstance(contributors, list):
                    for contributor in contributors:
                        if contributor.get("author", {}).get("login", "").lower() == username.lower():
                            for week in contributor.get("weeks", []):
                                total_lines += week.get("a", 0)
                break
            elif r.status_code == 202:
                # GitHub is computing stats — wait and retry
                wait = 2 * (2 ** attempt)
                print(f"  [202] Stats not ready for {repo_name}, retrying in {wait}s...")
                time.sleep(wait)

    return total_lines


def get_all_time_commits(username: str, token: str):
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
    response = make_request("post", graphql_url, headers=headers, json={"query": joined_query, "variables": {"login": username}})
    join_year = int(response.json()["data"]["user"]["createdAt"][:4])

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
        r = make_request("post", graphql_url, headers=headers, json={"query": query, "variables": {"login": username}})
        data = r.json()["data"]["user"]["contributionsCollection"]
        total_commits += data["totalCommitContributions"] + data["restrictedContributionsCount"]

    return total_commits


def get_all_time_contributed_to(username: str, token: str):
    headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}

    payload = {"query": REPOS_QUERY, "variables": {"login": username}}
    response = make_request("post", graphql_url, headers=headers, json=payload)
    owned_repos = {node["nameWithOwner"] for node in response.json()["data"]["user"]["repositories"]["nodes"]}

    joined_query = """
    query userInfo($login: String!) {
      user(login: $login) { createdAt }
    }
    """
    response = make_request("post", graphql_url, headers=headers, json={"query": joined_query, "variables": {"login": username}})
    join_year = int(response.json()["data"]["user"]["createdAt"][:4])

    external_repos = set()
    for year in range(join_year, datetime.now().year + 1):
        from_date = f"{year}-01-01T00:00:00Z"
        to_date = f"{year}-12-31T23:59:59Z"
        query = f"""
        query userInfo($login: String!) {{
          user(login: $login) {{
            contributionsCollection(from: "{from_date}", to: "{to_date}") {{
              commitContributionsByRepository(maxRepositories: 100) {{
                repository {{ nameWithOwner }}
              }}
            }}
          }}
        }}
        """
        r = make_request("post", graphql_url, headers=headers, json={"query": query, "variables": {"login": username}})
        contribs = r.json()["data"]["user"]["contributionsCollection"]["commitContributionsByRepository"]
        for contrib in contribs:
            name = contrib["repository"]["nameWithOwner"]
            if name not in owned_repos:
                external_repos.add(name)

    return len(owned_repos) + len(external_repos)


def percent_bar(percent: float, width: int = 20):
    percent = max(0, min(100, percent))
    filled = round((percent / 100) * width)
    empty = width - filled
    return f"[{'█' * filled}{'░' * empty}]"


def make_request(method: str, url: str, headers: dict, retries: int = 5, backoff: float = 2.0, **kwargs):
    """Make an HTTP request with exponential backoff retry on 5xx and rate-limit errors."""
    for attempt in range(retries):
        if method == "post":
            response = requests.post(url, headers=headers, **kwargs)
        else:
            response = requests.get(url, headers=headers, **kwargs)

        # Rate limited — back off and retry
        if response.status_code in (403, 429):
            body = response.json() if response.content else {}
            if "rate limit" in body.get("message", "").lower():
                wait = backoff * (2 ** attempt)
                print(f"  [rate limit] Retrying in {wait:.0f}s (attempt {attempt + 1}/{retries})...")
                time.sleep(wait)
                continue

        if response.status_code < 500:
            response.raise_for_status()
            return response

        wait = backoff * (2 ** attempt)
        print(f"  [{response.status_code}] Retrying in {wait:.0f}s (attempt {attempt + 1}/{retries})...")
        time.sleep(wait)

    response.raise_for_status()
    return response


if __name__ == "__main__":
    username = "lohiavardhan"
    token = os.getenv("GH_PAT", "")
    if not token:
        raise RuntimeError("GH_PAT is not set — requests will be unauthenticated and rate-limited.")
    generate_readme(username, token)
