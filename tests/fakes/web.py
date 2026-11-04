"""A stand-in for the browser-like web client: canned answers by address."""

import dataclasses
import json

from app.pipeline.errors import TryLater
from app.pipeline.web import Response


class FakeWeb:
    """`routes` maps an address (or the start of one, ending in *) to its answer; anything
    else is a 404. `calls` lists the addresses asked for, in order."""

    def __init__(self, routes: dict[str, Response] | None = None):
        self.routes = dict(routes or {})
        self.calls: list[str] = []

    def __call__(self) -> "FakeWeb":
        return self  # Downloader(web=...) wants a factory; one session is enough here

    def __enter__(self) -> "FakeWeb":
        return self

    def __exit__(self, *exc) -> None:
        pass

    def get(self, url: str, referer: str | None = None) -> Response:
        self.calls.append(url)
        answer = self.routes.get(url) or next(
            (
                response
                for prefix, response in self.routes.items()
                if prefix.endswith("*") and url.startswith(prefix[:-1])
            ),
            None,
        )
        if isinstance(answer, Exception):
            raise answer
        answer = answer or answer_with(404, b"not found")
        # No redirect unless the route names where it ends.
        return answer if answer.url else dataclasses.replace(answer, url=url)


def answer_with(
    status: int = 200,
    content: bytes = b"",
    content_type: str = "text/html",
    url: str = "",
    headers: dict[str, str] | None = None,
) -> Response:
    return Response(
        status=status,
        url=url,
        content_type=content_type,
        content=content,
        headers={"content-type": content_type, **(headers or {})},
    )


def html(body: str, url: str, status: int = 200) -> Response:
    return answer_with(status, body.encode(), "text/html", url)


def json_answer(data, url: str = "", status: int = 200) -> Response:
    return answer_with(status, json.dumps(data).encode(), "application/json", url)


def image(content: bytes, url: str = "", content_type: str = "image/png") -> Response:
    return answer_with(200, content, content_type, url)


UNREACHABLE = TryLater("Could not reach the site")
