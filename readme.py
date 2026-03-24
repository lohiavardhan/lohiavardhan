from datetime import datetime
import json
import os
import time
import requests

graphql_url = "https://api.github.com/graphql"

LOC_CACHE_FILE = "loc_cache.json"
LOC_CACHE_MAX_AGE_DAYS = 7

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
    repositories(ownerAffiliations: OWNER, first: 100) {
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

    # Shared repo discovery — used by both lines and contributed_to
    owned_repos, all_repos = discover_all_repos(username, token)

    lines = get_lines_of_code(username, token, all_repos)
    commits = get_all_time_commits(username, token)
    contributed_to = len(all_repos)

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


def discover_all_repos(username: str, token: str):
    """Return (owned_repo_names, all_repo_names) including external contributions.
    Called once and shared across functions that need the repo list."""
    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }

    # 1. Owned repos
    payload = {"query": REPOS_QUERY, "variables": {"login": username}}
    response = make_request("post", graphql_url, headers=headers, json=payload)
    owned_repos = {node["nameWithOwner"] for node in response.json()["data"]["user"]["repositories"]["nodes"]}

    # 2. Get join year
    joined_query = """
    query userInfo($login: String!) {
      user(login: $login) { createdAt }
    }
    """
    response = make_request("post", graphql_url, headers=headers, json={"query": joined_query, "variables": {"login": username}})
    join_year = int(response.json()["data"]["user"]["createdAt"][:4])

    # 3. Discover external repos across all years
    all_repos = set(owned_repos)
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
            all_repos.add(contrib["repository"]["nameWithOwner"])

    return owned_repos, all_repos


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


def get_lines_of_code(username: str, token: str, all_repos: set):
    """Count lines of code across all repos. Uses a local JSON cache that
    expires after LOC_CACHE_MAX_AGE_DAYS to avoid re-fetching every run."""

    # Check cache
    cached = load_loc_cache()
    if cached is not None:
        print(f"  [cache] Using cached LOC value: {cached:,}")
        return cached

    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }

    total_lines = 0

    for repo_name in all_repos:
        stats_url = f"https://api.github.com/repos/{repo_name}/stats/contributors"

        for attempt in range(3):  # reduced from 6 to 3
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

    save_loc_cache(total_lines)
    return total_lines


def load_loc_cache():
    """Load cached LOC value if the cache file exists and is less than
    LOC_CACHE_MAX_AGE_DAYS old. Returns the cached int or None."""
    if not os.path.exists(LOC_CACHE_FILE):
        return None
    try:
        with open(LOC_CACHE_FILE, "r") as f:
            data = json.load(f)
        cached_date = datetime.fromisoformat(data["date"])
        if (datetime.now() - cached_date).days < LOC_CACHE_MAX_AGE_DAYS:
            return data["lines"]
    except (json.JSONDecodeError, KeyError, ValueError):
        pass
    return None


def save_loc_cache(lines: int):
    """Persist the LOC count with a timestamp."""
    with open(LOC_CACHE_FILE, "w") as f:
        json.dump({"lines": lines, "date": datetime.now().isoformat()}, f)
    print(f"  [cache] Saved LOC cache: {lines:,}")


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