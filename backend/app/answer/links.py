from urllib.parse import quote

from app.models import Repo


def github_url(
    repo: Repo, path: str, start: int | None = None, end: int | None = None
) -> str:
    """Permalink to a file or line range at the indexed commit."""
    url = f"https://github.com/{repo.owner}/{repo.name}/blob/{repo.sha}/{quote(path)}"
    if start is None:
        return url
    if end is None or end == start:
        return f"{url}#L{start}"
    return f"{url}#L{start}-L{end}"
