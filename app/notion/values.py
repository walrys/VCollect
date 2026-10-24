"""Builders for the property values VCollect writes to Notion pages."""

# Notion's limits: 2,000 characters per text object, 100 objects or options per value.
TEXT_CHUNK = 2000
MAX_ITEMS = 100


def rich_text(text: str) -> dict:
    chunks = [text[i : i + TEXT_CHUNK] for i in range(0, len(text), TEXT_CHUNK)][:MAX_ITEMS]
    return {"rich_text": [{"type": "text", "text": {"content": chunk}} for chunk in chunks]}


def title(text: str) -> dict:
    return {"title": rich_text(text)["rich_text"]}


def url(link: str) -> dict:
    return {"url": link or None}


def multi_select(names: list[str]) -> dict:
    return {"multi_select": [{"name": name} for name in names[:MAX_ITEMS]]}


def status(name: str) -> dict:
    return {"status": {"name": name}}
