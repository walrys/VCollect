import asyncio

import pytest

from app.notion.schema import SchemaError, ensure_schema
from tests.fakes.notion import EXISTING_SCHEMA, FULL_SCHEMA, PROPS, FakeNotion, prop, status_prop


def ensure(fake: FakeNotion) -> None:
    asyncio.run(ensure_schema(fake, PROPS))


def test_missing_properties_are_added():
    fake = FakeNotion(EXISTING_SCHEMA)
    ensure(fake)

    types = {name: config["type"] for name, config in fake.properties.items()}
    assert types["Status"] == "status"
    assert types["Error"] == "rich_text"
    assert types["Summary"] == "rich_text"
    assert types["AI Tags"] == "multi_select"
    options = fake.properties["Status"]["status"]["options"]
    assert [(o["name"], o["group"]) for o in options] == [
        ("Not started", "To-do"),
        ("Done", "Complete"),
        ("Failed", "Complete"),
    ]


def test_a_complete_schema_is_left_alone():
    fake = FakeNotion(FULL_SCHEMA)
    ensure(fake)
    assert fake.property_updates == []


def test_an_existing_status_gains_only_the_missing_options():
    fake = FakeNotion({**FULL_SCHEMA, "Status": status_prop("Not started", "In progress", "Done")})
    ensure(fake)

    names = [o["name"] for o in fake.properties["Status"]["status"]["options"]]
    assert names == ["Not started", "In progress", "Done", "Failed"]
    sent = fake.property_updates[0]["Status"]["status"]["options"]
    assert sent[:3] == [{"id": "opt-Not started"}, {"id": "opt-In progress"}, {"id": "opt-Done"}]


def test_status_options_match_case_insensitively():
    fake = FakeNotion({**FULL_SCHEMA, "Status": status_prop("not started", "DONE", "failed")})
    ensure(fake)
    assert fake.property_updates == []


def test_the_title_column_can_be_the_caption():
    fake = FakeNotion({**FULL_SCHEMA, "Caption": prop("Caption", "title")})
    ensure(fake)
    assert fake.property_updates == []


def test_a_missing_url_property_is_reported():
    schema = {name: config for name, config in EXISTING_SCHEMA.items() if name != "URL"}
    with pytest.raises(SchemaError, match='no "URL" URL property'):
        ensure(FakeNotion(schema))


def test_a_property_of_the_wrong_type_is_reported():
    fake = FakeNotion({**EXISTING_SCHEMA, "Status": prop("Status", "select")})
    with pytest.raises(SchemaError, match='"Status" is a select property, expected status'):
        ensure(fake)
    assert fake.property_updates == []


def test_a_refused_status_creation_asks_the_user_to_add_it():
    fake = FakeNotion(EXISTING_SCHEMA)
    fake.refuse_status_creation = True
    with pytest.raises(SchemaError, match="Add it by hand as a Status property"):
        ensure(fake)
