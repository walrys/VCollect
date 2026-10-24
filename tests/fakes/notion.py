"""In-memory stand-in for NotionClient: a data source schema plus its rows."""

import copy
import itertools
from datetime import UTC, datetime, timedelta

from app.config import NotionProps
from app.notion.client import NotionError

PROPS = NotionProps(
    url="URL",
    caption="Caption",
    tags="Tags",
    status="Status",
    error="Error",
    summary="Summary",
    ai_tags="AI Tags",
)


def prop(name: str, kind: str, config: dict | None = None) -> dict:
    return {"id": f"id-{name}", "name": name, "type": kind, kind: config or {}}


def status_prop(*option_names: str) -> dict:
    options = [{"id": f"opt-{name}", "name": name} for name in option_names]
    return prop("Status", "status", {"options": options})


# The user's data source before VCollect touches it.
EXISTING_SCHEMA = {
    "Name": prop("Name", "title"),
    "URL": prop("URL", "url"),
    "Caption": prop("Caption", "rich_text"),
    "Tags": prop("Tags", "multi_select"),
}

FULL_SCHEMA = {
    **EXISTING_SCHEMA,
    "Status": status_prop("Not started", "Done", "Failed"),
    "Error": prop("Error", "rich_text"),
    "Summary": prop("Summary", "rich_text"),
    "AI Tags": prop("AI Tags", "multi_select"),
}


class FakeNotion:
    def __init__(self, schema: dict):
        self.data_source_id = "fake-data-source"
        self.properties = copy.deepcopy(schema)
        self.property_updates: list[dict] = []
        self.query_calls = 0
        self.capped = False  # answer queries as if Notion hit its 10,000-row limit
        self.refuse_status_creation = False
        self._rows: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._clock = datetime(2026, 10, 1, tzinfo=UTC)

    # NotionClient's interface

    async def retrieve_data_source(self) -> dict:
        return {"object": "data_source", "id": self.data_source_id, "properties": self.properties}

    async def update_data_source_properties(self, changes: dict) -> dict:
        creates_status = any(
            "status" in config and name not in self.properties for name, config in changes.items()
        )
        if self.refuse_status_creation and creates_status:
            raise NotionError(400, "validation_error", "Cannot create a status property.")
        self.property_updates.append(copy.deepcopy(changes))
        for name, config in changes.items():
            ((kind, settings),) = config.items()
            if kind == "status":
                settings = {"options": self._merge_options(name, settings["options"])}
            self.properties[name] = prop(name, kind, settings)
        return await self.retrieve_data_source()

    async def query_all_pages(self) -> tuple[list[dict], bool]:
        self.query_calls += 1
        pages = [self._page(page_id, row) for page_id, row in self._rows.items()]
        return pages, not self.capped

    async def aclose(self) -> None:
        pass

    # Test helpers, acting as the user editing Notion

    def add_row(self, **fields) -> str:
        page_id = f"page-{next(self._ids)}"
        now = self._tick()
        self._rows[page_id] = {"created": now, "edited": now, **fields}
        return page_id

    def edit_row(self, page_id: str, **fields) -> None:
        self._rows[page_id].update(fields, edited=self._tick())

    def delete_row(self, page_id: str) -> dict:
        return self._rows.pop(page_id)

    def restore_row(self, page_id: str, row: dict) -> None:
        self._rows[page_id] = row

    def created_time(self, page_id: str) -> str:
        return self._rows[page_id]["created"]

    def _merge_options(self, name: str, options: list[dict]) -> list[dict]:
        current = self.properties.get(name, {}).get("status", {}).get("options", [])
        by_id = {option["id"]: option for option in current}
        return [
            by_id[option["id"]]
            if "id" in option
            else {"id": f"opt-{option['name']}", "name": option["name"], "group": option["group"]}
            for option in options
        ]

    def _tick(self) -> str:
        self._clock += timedelta(minutes=1)
        return self._clock.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def _page(self, page_id: str, row: dict) -> dict:
        values = {
            "URL": {"type": "url", "url": row.get("url") or None},
            "Caption": _rich_text(
                row.get("caption", ""), self.properties.get("Caption", {}).get("type", "rich_text")
            ),
            "Tags": _multi_select(row.get("tags", [])),
            "Status": {
                "type": "status",
                "status": {"name": row["status"]} if row.get("status") else None,
            },
            "Error": _rich_text(row.get("error", "")),
            "Summary": _rich_text(row.get("summary", "")),
            "AI Tags": _multi_select(row.get("ai_tags", [])),
        }
        return {
            "object": "page",
            "id": page_id,
            "created_time": row["created"],
            "last_edited_time": row["edited"],
            "in_trash": False,
            # Like Notion, a page only carries the properties its data source has.
            "properties": {
                name: value for name, value in values.items() if name in self.properties
            },
        }


def _rich_text(text: str, kind: str = "rich_text") -> dict:
    """A rich_text property value, or a title one: both hold a rich text array."""
    parts = [{"type": "text", "text": {"content": text}, "plain_text": text}] if text else []
    return {"type": kind, kind: parts}


def _multi_select(names: list[str]) -> dict:
    return {"type": "multi_select", "multi_select": [{"name": name} for name in names]}
