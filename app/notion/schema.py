"""Checks the Notion data source and adds the properties VCollect writes."""

from app.config import NotionProps
from app.notion.client import NotionClient, NotionError

# Status option -> the Notion status group it belongs in.
STATUS_OPTIONS = {"Not started": "To-do", "Done": "Complete", "Failed": "Complete"}


class SchemaError(Exception):
    """The data source cannot be used as it is; the message tells the user what to fix."""


async def ensure_schema(client: NotionClient, props: NotionProps) -> None:
    existing = (await client.retrieve_data_source())["properties"]
    added_if_missing = {
        props.caption: "rich_text",
        props.tags: "multi_select",
        props.error: "rich_text",
        props.summary: "rich_text",
        props.ai_tags: "multi_select",
    }
    expected = {
        props.url: {"url"},
        props.status: {"status"},
        **{name: {kind} for name, kind in added_if_missing.items()},
        # The caption is often the database's title column.
        props.caption: {"rich_text", "title"},
    }

    problems = [
        f'"{name}" is a {existing[name]["type"]} property, expected {" or ".join(sorted(kinds))}'
        for name, kinds in expected.items()
        if name in existing and existing[name]["type"] not in kinds
    ]
    if props.url not in existing:
        problems.append(f'there is no "{props.url}" URL property')
    if problems:
        raise SchemaError("Fix these Notion properties: " + "; ".join(problems) + ".")

    changes = {name: {kind: {}} for name, kind in added_if_missing.items() if name not in existing}
    status_options = _status_options_to_set(existing.get(props.status))
    if status_options is not None:
        changes[props.status] = {"status": {"options": status_options}}
    if not changes:
        return

    try:
        await client.update_data_source_properties(changes)
    except NotionError as error:
        if props.status not in changes:
            raise
        raise SchemaError(
            f'Notion would not set up the "{props.status}" property ({error}). Add it by hand '
            "as a Status property with the options Not started, Done and Failed."
        ) from error


def _status_options_to_set(status: dict | None) -> list[dict] | None:
    """The full option list to send, or None when nothing is missing."""
    if status is None:
        return [{"name": name, "group": group} for name, group in STATUS_OPTIONS.items()]
    options = status["status"]["options"]
    have = {option["name"].lower() for option in options}
    missing = [name for name in STATUS_OPTIONS if name.lower() not in have]
    if not missing:
        return None
    # Notion drops any existing option left out of the list, so send them all back.
    kept = [{"id": option["id"]} for option in options]
    return kept + [{"name": name, "group": STATUS_OPTIONS[name]} for name in missing]
