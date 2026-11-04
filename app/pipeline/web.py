"""Plain web requests, for pages and their images: a browser-like client (curl_cffi, as
Chrome) carrying the sessions in cookies.txt. Also reports which sites cookies.txt covers."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path
from urllib.parse import urlsplit

from app.pipeline.errors import TryLater

TIMEOUT_SECONDS = 30
MAX_BYTES = 25 * 1024 * 1024  # anything longer is cut off: no page or image needs more


@dataclass
class Response:
    status: int
    url: str  # where the redirects ended
    content_type: str  # lower case, without parameters
    content: bytes
    headers: dict[str, str]  # names in lower case
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Web:
    """One browsing session, used as a context manager; cookies.txt is read as it opens, so a
    fresh export counts from the next post on."""

    def __init__(self, cookies: Path):
        self.cookies = cookies
        self._session = None

    def __enter__(self) -> "Web":
        from curl_cffi import requests

        self._session = requests.Session(impersonate="chrome", cookies=load_cookies(self.cookies))
        return self

    def __exit__(self, *exc) -> None:
        self._session.close()

    def get(self, url: str, referer: str | None = None) -> Response:
        """Raises TryLater when the site cannot be reached; any answer is returned as it is."""
        from curl_cffi import requests

        headers = {"Referer": referer} if referer else {}
        try:
            response = self._session.get(
                url, headers=headers, timeout=TIMEOUT_SECONDS, stream=True, max_redirects=10
            )
            chunks, size, truncated = [], 0, False
            for chunk in response.iter_content():
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    truncated = True
                    break
            response.close()
        except requests.RequestsError as error:
            raise TryLater(f"Could not reach {_host(url)}: {error}"[:500]) from error
        headers = {name.lower(): value for name, value in response.headers.items()}
        return Response(
            status=response.status_code,
            url=str(response.url),
            content_type=headers.get("content-type", "").split(";")[0].strip().lower(),
            content=b"".join(chunks),
            headers=headers,
            truncated=truncated,
        )


def load_cookies(path: Path) -> MozillaCookieJar:
    """Every cookie in cookies.txt, expired ones included; an empty jar when there is none."""
    jar = MozillaCookieJar()
    if path.exists():
        try:
            jar.load(str(path), ignore_discard=True, ignore_expires=True)
        except (LoadError, OSError):
            pass  # not a cookies.txt; the status line says so by finding no cookies
    return jar


def cookie_status(path: Path, url: str) -> dict:
    """Whether cookies.txt has cookies for the link's site, read without asking the site:
    state is missing, expired (all of them) or present; expires is the latest expiry, UTC."""
    host = _host(url)
    now = time.time()
    found = [cookie for cookie in load_cookies(path) if _covers(cookie.domain, host)]
    live = [cookie for cookie in found if not cookie.expires or cookie.expires > now]
    state = "present" if live else "expired" if found else "missing"
    dated = [cookie.expires for cookie in (live or found) if cookie.expires]
    expires = (
        datetime.fromtimestamp(max(dated), UTC).isoformat(timespec="seconds") if dated else None
    )
    return {"domain": host.removeprefix("www."), "state": state, "expires": expires}


def has_session(path: Path, url: str) -> bool:
    return cookie_status(path, url)["state"] == "present"


def _covers(domain: str, host: str) -> bool:
    domain = domain.lstrip(".").lower()
    return host == domain or host.endswith(f".{domain}")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
