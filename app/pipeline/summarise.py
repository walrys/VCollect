"""Summaries and AI tags from the local Ollama model."""

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.pipeline.errors import TryLater

MAX_TAGS = 5
MAX_TRANSCRIPT_CHARS = 8000
MAX_PAGE_CHARS = 12000  # a page's text, the post first and comments after
CONTEXT_TOKENS = 16384

SYSTEM = (
    "You catalogue saved social media posts for a personal, searchable archive. "
    "Always answer in English."
)

INSTRUCTIONS = """\
Describe this post in 2-3 plain sentences: what it shows, teaches or is about. \
Do not start with "This video" or "This post", and use no hashtags or emojis.

Then choose 3-5 tags: lowercase, singular nouns or short noun phrases \
(for example "recipe", "synthesizer", "game design"). Reuse tags from the existing \
list whenever one fits; invent a new tag only when none does.

Existing tags: {vocabulary}
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "tags"],
}


@dataclass
class PostContext:
    title: str
    uploader: str
    platform_caption: str
    user_caption: str
    transcript: str
    images: list[Path]  # video keyframes and post images, already scaled down
    vocabulary: list[str]  # existing AI tags, most used first
    page_text: str = ""  # the readable copy of a page or Reddit post


class Summariser:
    def __init__(
        self, base_url: str, model: str, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.base_url = base_url
        self.model = model
        self.transport = transport

    async def summarise(self, post: PostContext) -> tuple[str, list[str]]:
        message = {
            "role": "user",
            "content": prompt(post),
            "images": [base64.b64encode(path.read_bytes()).decode() for path in post.images],
        }
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM}, message],
            "format": RESPONSE_SCHEMA,
            "think": False,
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": CONTEXT_TOKENS},
        }
        try:
            async with httpx.AsyncClient(timeout=900, transport=self.transport) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=body)
                response.raise_for_status()
            answer = json.loads(response.json()["message"]["content"])
            summary = answer["summary"].strip()
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as error:
            raise TryLater(f"Summary failed: {error}") from error
        if not summary:
            raise TryLater("Summary failed: the model returned an empty summary")
        return summary, normalise_tags(answer.get("tags", []))


def prompt(post: PostContext) -> str:
    sections = [INSTRUCTIONS.format(vocabulary=", ".join(post.vocabulary) or "(none yet)")]
    for label, value in [
        ("Title", post.title),
        ("Uploader", post.uploader),
        ("Caption from the platform", post.platform_caption),
        ("The saver's own note", post.user_caption),
        ("Transcript", post.transcript[:MAX_TRANSCRIPT_CHARS]),
        ("Text of the page", _page_text(post)),
    ]:
        if value.strip():
            sections.append(f"{label}:\n{value.strip()}")
    if post.images:
        sections.append(f"{len(post.images)} images from the post are attached.")
    return "\n\n".join(sections)


def _page_text(post: PostContext) -> str:
    """A page without a readable copy has its description as its text: once is enough."""
    if post.page_text.strip() == post.platform_caption.strip():
        return ""
    return post.page_text[:MAX_PAGE_CHARS]


def normalise_tags(tags: list) -> list[str]:
    seen: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            continue
        clean = re.sub(r"\s+", " ", tag.strip().lstrip("#").strip(" .,;:!?\"'").lower())
        if clean and len(clean) <= 40 and clean not in seen:
            seen.append(clean)
    return seen[:MAX_TAGS]
