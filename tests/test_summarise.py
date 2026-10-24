import asyncio
import json

import httpx
import pytest

from app.pipeline.errors import TryLater
from app.pipeline.summarise import PostContext, Summariser, normalise_tags


def context(tmp_path, **overrides):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg bytes")
    fields = dict(
        title="Pasta trick",
        uploader="chef",
        platform_caption="#pasta #italian",
        user_caption="try this",
        transcript="so today we make pasta",
        images=[frame],
        vocabulary=["recipe", "cooking"],
    )
    return PostContext(**{**fields, **overrides})


def summarise(post, handler):
    summariser = Summariser("http://ollama:11434", "qwen3.5:9b", httpx.MockTransport(handler))
    return asyncio.run(summariser.summarise(post))


def reply(content):
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


def test_the_request_carries_the_post_and_asks_for_json(tmp_path):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return reply(json.dumps({"summary": " A pasta trick. ", "tags": ["Recipe", "#Pasta"]}))

    summary, tags = summarise(context(tmp_path), handler)

    assert (summary, tags) == ("A pasta trick.", ["recipe", "pasta"])
    body = sent[0]
    assert body["model"] == "qwen3.5:9b"
    assert body["think"] is False and body["stream"] is False
    assert body["format"]["required"] == ["summary", "tags"]
    user = body["messages"][1]
    assert len(user["images"]) == 1
    for expected in ("Pasta trick", "so today we make pasta", "try this", "recipe, cooking"):
        assert expected in user["content"]


def test_empty_fields_are_left_out_of_the_prompt(tmp_path):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return reply(json.dumps({"summary": "x", "tags": []}))

    summarise(context(tmp_path, title="", transcript="", images=[]), handler)
    content = sent[0]["messages"][1]["content"]
    assert "Title:" not in content and "Transcript:" not in content
    assert "images" not in sent[0]["messages"][1] or sent[0]["messages"][1]["images"] == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        reply("not json"),
        reply(json.dumps({"tags": ["x"]})),
        reply(json.dumps({"summary": "  ", "tags": []})),
    ],
)
def test_bad_answers_mean_try_later(tmp_path, response):
    with pytest.raises(TryLater):
        summarise(context(tmp_path), lambda request: response)


def test_tags_are_cleaned_deduplicated_and_capped():
    raw = ["Game Design", "game  design", "#Synth!", "", 7, "a" * 41, "one", "two", "three", "four"]
    assert normalise_tags(raw) == ["game design", "synth", "one", "two", "three"]
