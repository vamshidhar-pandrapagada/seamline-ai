"""Phase 2 extraction: schema, chunking, quote verification, and the pipeline with a fake model."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from seamline.config import parse_config
from seamline.extract.chunker import build_chunks
from seamline.extract.extractor import extract_events, system_prompt
from seamline.extract.providers import (
    AnthropicProvider,
    FakeProvider,
    ProviderAuthError,
    ProviderError,
)
from seamline.extract.schema import FACT_JSON_SCHEMA, ExtractedFact, ExtractionOutput
from seamline.extract.verify import normalize, verify
from seamline.transcripts.classify import classify
from seamline.transcripts.models import Event, Kind
from seamline.transcripts.reader import read_events

FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "basic.jsonl"
ROOT = Path("/work/shop")


def config():
    return parse_config(
        {
            "project": "shop",
            "services": {"orders": "services/orders", "payments": "services/payments"},
        },
        root=ROOT,
    )


def fact(**kw) -> dict:
    base = {
        "kind": "provides",
        "claim": "orders emits order.created with amount_cents as an integer.",
        "interface": "order.created",
        "interface_kind": "event",
        "service": "orders",
        "details": [{"name": "amount_cents", "value": "integer"}],
        "quote": "emit order.created with amount_cents as an integer",
        "line": 3,
        "confidence": 0.9,
    }
    return {**base, **kw}


def prompt(line, text, uuid=None):
    return Event(line_no=line, block=0, kind=Kind.USER_PROMPT, text=text, uuid=uuid or f"u{line}")


def reply(line, text):
    return Event(line_no=line, block=0, kind=Kind.ASSISTANT_TEXT, text=text, uuid=f"u{line}")


def edit(line, path):
    return Event(
        line_no=line,
        block=0,
        kind=Kind.TOOL_USE,
        tool_name="Edit",
        tool_input={"file_path": path},
        uuid=f"u{line}",
    )


def labeled(events):
    return [(e, classify(e)) for e in events]


# --- schema -------------------------------------------------------------------------------


def test_schema_accepts_valid_and_rejects_invalid():
    assert ExtractionOutput.model_validate({"facts": [fact()]}).facts[0].kind == "provides"
    for bad in (
        fact(kind="finding"),
        fact(confidence=1.5),
        {**fact(), "extra": 1},
        fact(interface_kind="grpc-ish"),
    ):
        with pytest.raises(ValueError):
            ExtractionOutput.model_validate({"facts": [bad]})


def test_json_schema_is_strict_everywhere():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(FACT_JSON_SCHEMA)
    item = FACT_JSON_SCHEMA["properties"]["facts"]["items"]["properties"]
    assert set(item) == set(ExtractedFact.model_fields)


def test_system_prompt_is_packaged():
    assert "dead_end" in system_prompt()


# --- chunking -----------------------------------------------------------------------------


def test_chunks_from_fixture_exclude_tool_output():
    events = read_events(FIXTURE).events
    (chunk,) = build_chunks(labeled(events), root=ROOT)
    assert "[L3] USER: orders should emit order.created" in chunk.text
    assert "[L5] CLAUDE: I'll update the event schema" in chunk.text
    assert "[L6] (Claude edited services/orders/events.py)" in chunk.text
    assert "3 passed" not in chunk.text  # tool result
    assert "Let me look at the emitter" not in chunk.text  # thinking
    assert "live_abc123" not in chunk.text  # .env content
    assert 6 not in chunk.quotable  # anchors are context only


def test_chunks_split_on_turns_within_budget():
    events = []
    for turn in range(10):
        events += [
            prompt(turn * 10, f"question {turn} " + "x" * 300),
            reply(turn * 10 + 1, f"answer {turn} " + "y" * 300),
        ]
    chunks = build_chunks(labeled(events), budget_tokens=400)  # ~1600 chars
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.text) <= 1600
        first_line = c.text.splitlines()[0]
        assert " USER: " in first_line  # every excerpt starts at a turn boundary


def test_oversized_event_is_split_with_same_line_tag():
    chunks = build_chunks(labeled([prompt(7, "z" * 5000)]), budget_tokens=300)
    assert len(chunks) > 1
    assert all(c.text.startswith("[L7] USER: ") for c in chunks)
    assert all(c.quotable[7] == "z" * 5000 for c in chunks)


def test_chunk_with_only_anchors_is_dropped():
    assert build_chunks(labeled([edit(1, "/a.py")])) == []


# --- verification -------------------------------------------------------------------------


def chunk_for(*events):
    (c,) = build_chunks(labeled(list(events)), root=ROOT)
    return c


def parsed(**kw):
    return ExtractedFact.model_validate(fact(**kw))


def test_verify_exact_and_formatting_tolerance():
    c = chunk_for(
        prompt(3, "orders should emit **order.created** with `amount_cents`\n as an integer")
    )
    assert verify(parsed(quote="emit order.created with amount_cents as an integer"), c).ok
    assert verify(parsed(quote="EMIT Order.Created   with amount_cents as an integer"), c).ok


def test_verify_rejects_paraphrase_and_short_quotes():
    c = chunk_for(prompt(3, "orders should emit order.created with amount_cents as an integer"))
    assert not verify(parsed(quote="emit order.created with the amount in cents"), c).ok
    assert verify(parsed(quote="emit order"), c).reason == "quote too short"
    assert not verify(parsed(quote="emit order.created with amountcents as an integer"), c).ok


def test_verify_corrects_line_but_never_quotes_anchors():
    c = chunk_for(
        prompt(3, "hello there, general question"),
        reply(4, "orders will emit order.created with amount_cents as an integer"),
        edit(5, "/work/shop/services/orders/events.py"),
    )
    v = verify(parsed(line=3), c)
    assert v.ok and v.line == 4 and v.reason == "line corrected"
    assert not verify(parsed(quote="Claude edited services/orders/events.py", line=5), c).ok


def test_normalize():
    assert normalize("  “Hi”  **there**\n`x_y` ") == "hi there x_y"
    assert (
        normalize("steps:\n- first | second\n1. third\n## Head") == "steps: first second third head"
    )


def test_verify_list_items_and_ellipsis_pieces():
    text = (
        "Hooks play two roles:\n- Go read the transcript now (Stop).\n"
        "- Tell Claude what the ledger knows."
    )
    c = chunk_for(reply(4, text))
    assert verify(parsed(quote="Go read the transcript now (Stop). Tell Claude what", line=4), c).ok
    assert verify(
        parsed(quote="Hooks play two roles … Tell Claude what the ledger knows", line=4), c
    ).ok
    # Pieces must be in order and each at least three words
    assert not verify(
        parsed(quote="Tell Claude what the ledger … Hooks play two roles", line=4), c
    ).ok
    assert (
        verify(parsed(quote="Hooks play … ledger knows", line=4), c).reason
        == "quote pieces too short"
    )
    # Dropping words from the middle is still a paraphrase
    assert not verify(
        parsed(quote="Go read the transcript now. Tell Claude what the ledger knows", line=4), c
    ).ok


# --- pipeline -----------------------------------------------------------------------------


def test_extract_with_fake_provider():
    events = read_events(FIXTURE).events
    good = fact()
    moved = fact(
        quote="update the event schema so amount_cents is an int",
        line=99,
        kind="decision",
        interface=None,
        interface_kind=None,
        service="billing",
    )
    invented = fact(quote="payments reads amount as a float", kind="assumes")
    provider = FakeProvider([{"facts": [good, moved, invented]}])
    report = extract_events(events, config(), provider, session_id="s1")

    assert report.chunks == report.chunks_done == 1
    assert [f.line for f in report.facts] == [3, 5]
    assert report.facts[1].fact.service is None  # "billing" isn't a listed service
    assert report.facts[0].timestamp and report.facts[0].cwd == "/work/shop/services/orders"
    assert [r.reason for r in report.rejected] == ["quote not found in transcript"]
    assert "Services (name: folder):\n- orders: services/orders/" in provider.prompts[0]


def test_extract_records_errors_and_continues():
    events = [prompt(1, "a" * 3000), reply(2, "b" * 3000), prompt(3, "c" * 3000)]
    provider = FakeProvider([ProviderError("boom"), {"not": "facts"}, {"facts": []}])
    report = extract_events(events, config(), provider, session_id="s", budget_tokens=900)
    assert report.chunks == 3 and report.chunks_done == 2
    assert "boom" in report.errors[0]
    assert "invalid output" in report.errors[1]


def test_extract_skips_inherited_history_and_limits_chunks():
    events = [
        prompt(1, "old question from the parent session", uuid="old"),
        prompt(2, "new question " + "q" * 3000),
    ]
    provider = FakeProvider(lambda p: {"facts": []})
    extract_events(events, config(), provider, session_id="s", inherited={"old"})
    assert "old question" not in provider.prompts[0]
    provider.prompts.clear()
    report = extract_events(
        [prompt(i, "p" * 3000) for i in range(5)],
        config(),
        provider,
        session_id="s",
        budget_tokens=900,
        max_chunks=2,
    )
    assert report.chunks == 5 and len(provider.prompts) == 2


# --- Anthropic provider (no network: a stand-in client) ----------------------------------


class StubClient:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.messages = self
        self.beta = SimpleNamespace(messages=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def api_response(text, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=120, output_tokens=40),
        model="claude-haiku-4-5",
    )


def tool_response(data, stop_reason="tool_use"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="tool_use", name="record_facts", input=data)],
        usage=SimpleNamespace(input_tokens=120, output_tokens=40),
        model="claude-haiku-4-5",
    )


def test_anthropic_provider_tool_mode_is_default():
    client = StubClient(tool_response({"facts": [fact()]}))
    out = AnthropicProvider("claude-haiku-4-5", client=client).complete("sys", "excerpt", {"x": 1})
    call = client.calls[0]
    assert call["tools"][0]["input_schema"] == {"x": 1}
    assert call["tools"][0]["strict"] is True
    assert call["tool_choice"] == {"type": "tool", "name": "record_facts"}
    assert "output_config" not in call
    assert out.data["facts"][0]["kind"] == "provides"


def test_anthropic_provider_tool_mode_without_tool_call():
    client = StubClient(api_response("I found nothing."))
    with pytest.raises(ProviderError, match="did not call"):
        AnthropicProvider("m", client=client).complete("s", "p", {})


def test_anthropic_provider_schema_mode_sends_output_config():
    client = StubClient(api_response(json.dumps({"facts": [fact()]})))
    provider = AnthropicProvider("claude-haiku-4-5", client=client, mode="schema")
    out = provider.complete("sys", "excerpt", {"x": 1})
    call = client.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["system"] == "sys"
    assert call["output_config"] == {"format": {"type": "json_schema", "schema": {"x": 1}}}
    assert out.data["facts"][0]["kind"] == "provides"
    assert (out.input_tokens, out.output_tokens) == (120, 40)


@pytest.mark.parametrize(
    "response, message",
    [
        (api_response("{}", stop_reason="refusal"), "declined"),
        (api_response('{"facts": [', stop_reason="max_tokens"), "max_tokens"),
        (api_response("not json"), "invalid JSON"),
    ],
)
def test_anthropic_provider_errors(response, message):
    with pytest.raises(ProviderError, match=message):
        AnthropicProvider("m", client=StubClient(response), mode="schema").complete("s", "p", {})


def test_anthropic_provider_without_credentials(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("ANTHROPIC_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no `ant auth login` profile either
    provider = AnthropicProvider("claude-haiku-4-5")
    with pytest.raises(ProviderAuthError, match="ANTHROPIC_API_KEY"):
        provider.complete("s", "p", {"type": "object"})


def test_missing_credentials_stop_extraction_at_first_excerpt():
    events = [prompt(i, "p" * 3000) for i in range(4)]
    provider = FakeProvider([ProviderAuthError("no key")] * 4)
    report = extract_events(events, config(), provider, session_id="s", budget_tokens=900)
    assert report.errors == ["no key"]
    assert len(provider.prompts) == 1


def test_opus_uses_server_side_fallback_other_models_do_not():
    client = StubClient(tool_response({"facts": []}))
    AnthropicProvider("claude-opus-5", client=client).complete("s", "p", {})
    assert client.calls[-1]["fallbacks"] == "default"
    assert client.calls[-1]["betas"] == ["server-side-fallback-2026-07-01"]
    AnthropicProvider("claude-haiku-4-5", client=client).complete("s", "p", {})
    assert "fallbacks" not in client.calls[-1] and "betas" not in client.calls[-1]
