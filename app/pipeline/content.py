"""What a download brings back: media items, a readable copy of the page, or both."""

import json
from dataclasses import asdict, dataclass


@dataclass
class RawItem:
    file: str  # relative to the download folder
    kind: str  # video | image


@dataclass
class PageCopy:
    """The page in readable form. `html` is sanitised, and its images point at `images`, files
    in the download folder; it is empty when the page had nothing to read without JavaScript."""

    html: str
    text: str  # plain text, for search and the summary
    images: list[str]


@dataclass
class Download:
    items: list[RawItem]  # empty for a page with no media of its own
    title: str
    uploader: str
    caption: str  # the platform's caption, or the page's description
    subtitles: str | None  # the platform's own subtitles as plain text
    thumbnail: str | None  # relative to the download folder
    page: PageCopy | None = None
    url: str | None = None  # the post's canonical link, once a share link is resolved
    # A video left on its site (a YouTube video that is not a Short): its thumbnail, captions
    # and details, no items, until the video is asked for.
    preview: bool = False
    duration: float | None = None  # a preview's length, in seconds

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> "Download":
        data = json.loads(text)
        page = data.get("page")
        return cls(
            **{
                **data,
                "items": [RawItem(**item) for item in data["items"]],
                "page": PageCopy(**page) if page else None,
            }
        )
