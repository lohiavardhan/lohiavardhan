from datetime import datetime, timezone, timedelta
import json
import os
import time
import requests

GRAPHQL_URL = "https://api.github.com/graphql"
STATE_FILE = "state.json"
INCREMENTAL_WINDOW_HOURS = 12

# ─── GraphQL Queries ──────────────────────────────────────────────────────────

STATS_QUERY = """
query userInfo($login: String!) {
  user(login: $login) {
    name
    login
    followers { totalCount }
    repositories(first: 100, ownerAffiliations: OWNER) {
      totalCount
      nodes {
        name
        stargazers { totalCount }
      }
    }
  }
}
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
            node { color name }
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
      nodes { nameWithOwner }
    }
  }
}
"""

JOIN_YEAR_QUERY = """
query userInfo($login: String!) {
  user(login: $login) { createdAt }
}
"""


# ─── State Management ─────────────────────────────────────────────────────────

def load_state() -> dict | None:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return None


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def should_run_incremental(state: dict | None) -> bool:
    """Incremental if we have a valid prior state."""
    if state is None or not state.get("initialized"):
        return False
    return True


# ─── Shared Helpers ────────────────────────────────────────────────────────────

def get_join_year(username: str, headers: dict) -> int:
    r = make_request("post", GRAPHQL_URL, headers=headers,
                     json={"query": JOIN_YEAR_QUERY, "variables": {"login": username}})
    return int(r.json()["data"]["user"]["createdAt"][:4])


def get_commits_in_range(username: str, headers: dict, from_iso: str, to_iso: str) -> int:
    """Get total commits (public + private) in a date range."""
    query = f"""
    query userInfo($login: String!) {{
      user(login: $login) {{
        contributionsCollection(from: "{from_iso}", to: "{to_iso}") {{
          totalCommitContributions
          restrictedContributionsCount
        }}
      }}
    }}
    """
    r = make_request("post", GRAPHQL_URL, headers=headers,
                     json={"query": query, "variables": {"login": username}})
    data = r.json()["data"]["user"]["contributionsCollection"]
    return data["totalCommitContributions"] + data["restrictedContributionsCount"]


def get_repos_in_range(username: str, headers: dict, from_iso: str, to_iso: str) -> set:
    """Discover repos contributed to in a date range."""
    query = f"""
    query userInfo($login: String!) {{
      user(login: $login) {{
        contributionsCollection(from: "{from_iso}", to: "{to_iso}") {{
          commitContributionsByRepository(maxRepositories: 100) {{
            repository {{ nameWithOwner }}
          }}
        }}
      }}
    }}
    """
    r = make_request("post", GRAPHQL_URL, headers=headers,
                     json={"query": query, "variables": {"login": username}})
    contribs = r.json()["data"]["user"]["contributionsCollection"]["commitContributionsByRepository"]
    return {c["repository"]["nameWithOwner"] for c in contribs}


def fetch_repo_loc(username: str, headers: dict, repo_name: str) -> int | None:
    """Fetch lines added by the user in a single repo. Returns None if stats aren't ready."""
    stats_url = f"https://api.github.com/repos/{repo_name}/stats/contributors"
    for attempt in range(3):
        r = make_request("get", stats_url, headers=headers)
        if r.status_code == 200:
            contributors = r.json()
            if isinstance(contributors, list):
                lines = 0
                for contributor in contributors:
                    if contributor.get("author", {}).get("login", "").lower() == username.lower():
                        for week in contributor.get("weeks", []):
                            lines += week.get("a", 0)
                return lines
            return 0
        elif r.status_code == 202:
            wait = 2 * (2 ** attempt)
            print(f"  [202] Stats not ready for {repo_name}, retrying in {wait}s...")
            time.sleep(wait)
    return None  # never got a 200


# ─── Full Run ──────────────────────────────────────────────────────────────────

def full_run(username: str, token: str) -> dict:
    """First-time run: compute everything from scratch."""
    print("═══ FULL RUN ═══")
    headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}

    # 1. Discover all repos (owned + external, all years)
    print("▸ Discovering repos...")
    payload = {"query": REPOS_QUERY, "variables": {"login": username}}
    r = make_request("post", GRAPHQL_URL, headers=headers, json=payload)
    owned_repos = {n["nameWithOwner"] for n in r.json()["data"]["user"]["repositories"]["nodes"]}

    join_year = get_join_year(username, headers)
    all_repos = set(owned_repos)
    for year in range(join_year, datetime.now().year + 1):
        from_d = f"{year}-01-01T00:00:00Z"
        to_d = f"{year}-12-31T23:59:59Z"
        all_repos |= get_repos_in_range(username, headers, from_d, to_d)
    print(f"  Found {len(all_repos)} repos ({len(owned_repos)} owned)")

    # 2. All-time commits (year by year)
    print("▸ Counting commits...")
    total_commits = 0
    for year in range(join_year, datetime.now().year + 1):
        from_d = f"{year}-01-01T00:00:00Z"
        to_d = f"{year}-12-31T23:59:59Z"
        total_commits += get_commits_in_range(username, headers, from_d, to_d)
    print(f"  Total commits: {total_commits:,}")

    # 3. Lines of code (all repos)
    print("▸ Counting lines of code...")
    loc_cache = {}
    loc_missed = []
    total_loc = 0
    for repo_name in all_repos:
        lines = fetch_repo_loc(username, headers, repo_name)
        if lines is not None:
            loc_cache[repo_name] = lines
            total_loc += lines
        else:
            loc_missed.append(repo_name)
            print(f"  [miss] No data for {repo_name}")
    print(f"  Total LOC: {total_loc:,} ({len(loc_missed)} repo(s) missed — will retry next run)")

    # 4. Languages (cheap, always fetch fresh)
    languages = get_languages(username, token)

    state = {
        "initialized": True,
        "last_run": datetime.now(timezone.utc).isoformat(),
        "total_commits": total_commits,
        "total_loc": total_loc,
        "loc_cache": loc_cache,
        "loc_missed": loc_missed,
        "all_repos": sorted(all_repos),
        "languages": languages,
    }
    save_state(state)
    return state


# ─── Incremental Run ──────────────────────────────────────────────────────────

def incremental_run(username: str, token: str, prev_state: dict) -> dict:
    """Subsequent runs: only look at the window since last run."""
    print("═══ INCREMENTAL RUN ═══")
    headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}

    last_run = prev_state["last_run"]
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    last_run_dt = datetime.fromisoformat(last_run)
    years_to_check = set()
    years_to_check.add(last_run_dt.year)
    years_to_check.add(now.year)

    # 1. Incremental commits — only the window years
    print(f"▸ Commits since {last_run}...")
    new_total_commits = 0
    for year in range(min(years_to_check), max(years_to_check) + 1):
        from_d = f"{year}-01-01T00:00:00Z"
        to_d = f"{year}-12-31T23:59:59Z"
        new_total_commits += get_commits_in_range(username, headers, from_d, to_d)

    join_year = get_join_year(username, headers)
    per_year_commits = prev_state.get("per_year_commits", {})

    for year in years_to_check:
        from_d = f"{year}-01-01T00:00:00Z"
        to_d = f"{year}-12-31T23:59:59Z"
        per_year_commits[str(year)] = get_commits_in_range(username, headers, from_d, to_d)

    total_commits = sum(per_year_commits.values())
    print(f"  Total commits: {total_commits:,} (refreshed years: {sorted(years_to_check)})")

    # 2. Discover any new repos in the window
    print("▸ Checking for new repos...")
    all_repos = set(prev_state["all_repos"])
    for year in years_to_check:
        from_d = f"{year}-01-01T00:00:00Z"
        to_d = f"{year}-12-31T23:59:59Z"
        all_repos |= get_repos_in_range(username, headers, from_d, to_d)

    new_repos = all_repos - set(prev_state["all_repos"])
    if new_repos:
        print(f"  Found {len(new_repos)} new repo(s): {new_repos}")

    # 3. LOC — re-fetch repos with recent activity + retry previously missed repos
    print("▸ Updating LOC for active repos...")
    loc_cache = dict(prev_state.get("loc_cache", {}))
    prev_missed = set(prev_state.get("loc_missed", []))
    recently_active = get_recently_active_repos(username, headers)

    repos_to_refresh = ((recently_active | new_repos) & all_repos) | prev_missed
    if prev_missed:
        print(f"  Retrying {len(prev_missed)} previously missed repo(s)")
    print(f"  Refreshing {len(repos_to_refresh)} repo(s) total")

    still_missed = []
    for repo_name in repos_to_refresh:
        lines = fetch_repo_loc(username, headers, repo_name)
        if lines is not None:
            loc_cache[repo_name] = lines
            if repo_name in prev_missed:
                print(f"  [recovered] {repo_name}: {lines:,} lines")
        else:
            still_missed.append(repo_name)
            if repo_name not in loc_cache:
                print(f"  [miss] No data for {repo_name}")

    total_loc = sum(loc_cache.get(r, 0) for r in all_repos)
    print(f"  Total LOC: {total_loc:,} ({len(still_missed)} repo(s) still missed)")

    # 4. Languages (single GraphQL call — always fresh)
    languages = get_languages(username, token)

    state = {
        "initialized": True,
        "last_run": now_iso,
        "total_commits": total_commits,
        "per_year_commits": per_year_commits,
        "total_loc": total_loc,
        "loc_cache": loc_cache,
        "loc_missed": still_missed,
        "all_repos": sorted(all_repos),
        "languages": languages,
    }
    save_state(state)
    return state


def get_recently_active_repos(username: str, headers: dict) -> set:
    """Use the Events API to find repos the user pushed to recently."""
    events_url = f"https://api.github.com/users/{username}/events?per_page=100"
    active = set()
    try:
        r = make_request("get", events_url, headers=headers)
        if r.status_code == 200:
            for event in r.json():
                if event.get("type") == "PushEvent":
                    repo = event.get("repo", {}).get("name")
                    if repo:
                        active.add(repo)
    except Exception:
        pass  # non-critical — worst case we skip some LOC updates
    return active


# ─── Helpers ─────────────────────────────────────────────────────────

def get_stats(username: str, token: str) -> dict:
    headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}
    payload = {"query": STATS_QUERY, "variables": {"login": username}}
    response = make_request("post", GRAPHQL_URL, headers=headers, json=payload)
    data = response.json()["data"]["user"]
    stars = sum(repo["stargazers"]["totalCount"] for repo in data["repositories"]["nodes"])
    return {"stars": stars, "followers": data["followers"]["totalCount"]}


def get_languages(username: str, token: str) -> dict:
    headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}
    payload = {"query": LANGUAGES_QUERY, "variables": {"login": username}}
    response = make_request("post", GRAPHQL_URL, headers=headers, json=payload)
    data = response.json()["data"]["user"]["repositories"]["nodes"]

    languages = {}
    for repo in data:
        for edge in repo["languages"]["edges"]:
            lang_name = edge["node"]["name"]
            lang_size = edge["size"]
            languages[lang_name] = languages.get(lang_name, 0) + lang_size

    languages["Python"] = languages.get("Python", 0) + languages.pop("Jupyter Notebook", 0)
    return dict(sorted(languages.items(), key=lambda pair: pair[1], reverse=True))


def percent_bar(percent: float, width: int = 20) -> str:
    percent = max(0, min(100, percent))
    filled = round((percent / 100) * width)
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def make_request(method: str, url: str, headers: dict, retries: int = 5, backoff: float = 2.0, **kwargs):
    for attempt in range(retries):
        if method == "post":
            response = requests.post(url, headers=headers, **kwargs)
        else:
            response = requests.get(url, headers=headers, **kwargs)

        if response.status_code in (403, 429):
            body = response.json() if response.content else {}
            if "rate limit" in body.get("message", "").lower():
                wait = backoff * (2 ** attempt)
                print(f"  [rate limit] Retrying in {wait:.0f}s (attempt {attempt + 1}/{retries})...")
                time.sleep(wait)
                continue

        if response.status_code < 500:
            return response  # includes 200, 202, 204, etc.

        wait = backoff * (2 ** attempt)
        print(f"  [{response.status_code}] Retrying in {wait:.0f}s (attempt {attempt + 1}/{retries})...")
        time.sleep(wait)

    return response


# ─── README Writer ─────────────────────────────────────────────────────────────

def write_readme(state: dict, path: str = "README.md"):
    languages = state["languages"]
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"> last updated: {datetime.now().strftime('%d %b %Y, %H:%M UTC')}\n\n")
        f.write("# i'm vardhan, i write code \n\n")
        f.write("---\n\n")

        f.write("## 📊 stats\n")
        f.write("```\n")
        f.write(f"commits:               {state['total_commits']:,}\n")
        f.write(f"contributed to:        {len(state['all_repos'])} repos\n")
        f.write(f"lines of code written: {state['total_loc']:,}\n")
        f.write("```\n\n")

        f.write("## 💻 top languages\n")
        f.write("```\n")
        total_size = sum(languages.values())
        for lang, size in list(languages.items())[:10]:
            percent = (size / total_size) * 100 if total_size > 0 else 0
            bar = percent_bar(percent)
            f.write(f"{lang:<12} {bar} {percent:.2f}%\n")
        f.write("```\n")


# ─── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    username = "lohiavardhan"
    token = os.getenv("GH_PAT", "")
    if not token:
        raise RuntimeError("GH_PAT is not set — requests will be unauthenticated and rate-limited.")

    prev_state = load_state()

    if should_run_incremental(prev_state):
        state = incremental_run(username, token, prev_state)
    else:
        state = full_run(username, token)
        # After the first full run, also backfill per-year commits so
        # incremental runs can replace individual years cleanly.
        if "per_year_commits" not in state:
            print("▸ Backfilling per-year commit cache...")
            headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}
            join_year = get_join_year(username, headers)
            per_year = {}
            for year in range(join_year, datetime.now().year + 1):
                from_d = f"{year}-01-01T00:00:00Z"
                to_d = f"{year}-12-31T23:59:59Z"
                per_year[str(year)] = get_commits_in_range(username, headers, from_d, to_d)
            state["per_year_commits"] = per_year
            save_state(state)

    write_readme(state)
    print(f"\n✓ README.md updated")


if __name__ == "__main__":
    main()
