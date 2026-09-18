import json
from pathlib import Path

from seamline.transcripts.models import Kind
from seamline.transcripts.reader import read_events
from seamline.transcripts.redact import MASK

FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "basic.jsonl"


def by_uuid(events):
    return {e.uuid: e for e in events if e.uuid}


def test_kinds():
    events = by_uuid(read_events(FIXTURE).events)
    expected = {
        "u-att": Kind.ATTACHMENT,
        "u-prompt": Kind.USER_PROMPT,
        "u-think": Kind.THINKING,
        "u-text": Kind.ASSISTANT_TEXT,
        "u-edit": Kind.TOOL_USE,
        "u-edit-res": Kind.TOOL_RESULT,
        "u-bash": Kind.TOOL_USE,
        "u-bash-res": Kind.TOOL_RESULT,
        "u-interrupt": Kind.INTERRUPT,
        "u-meta": Kind.META,
        "u-cmd": Kind.META,
        "u-boundary": Kind.SYSTEM,
        "u-summary": Kind.COMPACT_SUMMARY,
        "u-stophook": Kind.SYSTEM,
    }
    assert {u: events[u].kind for u in expected} == expected


def test_fields():
    result = read_events(FIXTURE)
    events = by_uuid(result.events)
    edit = events["u-edit"]
    assert edit.tool_name == "Edit"
    assert edit.file_path == "/work/shop/services/orders/events.py"
    assert edit.tool_use_id == "t-edit"
    assert events["u-bash"].file_path is None
    assert events["u-bash"].text == "pytest -q"
    assert events["u-bash-res"].text == "3 passed"
    assert events["u-prompt"].cwd == "/work/shop/services/orders"
    assert events["u-prompt"].line_no == 3
    assert result.line_no == 20
    assert result.offset == FIXTURE.stat().st_size
    assert result.bad_lines == []


def test_redaction_applied():
    events = by_uuid(read_events(FIXTURE).events)
    assert "sk-ant" not in events["u-secret"].text
    assert MASK in events["u-secret"].text
    assert events["u-env-res"].text == MASK  # Whole .env content, not just secret-looking parts


def test_incremental_and_partial_line(tmp_path):
    path = tmp_path / "s.jsonl"
    lines = FIXTURE.read_text().splitlines(keepends=True)
    path.write_text("".join(lines[:5]) + lines[5][:30])  # line 6 half-written

    first = read_events(path)
    assert first.line_no == 5
    assert max(e.line_no for e in first.events) == 5

    path.write_text("".join(lines))  # the writer finishes
    second = read_events(path, first.offset, first.line_no)
    assert min(e.line_no for e in second.events) == 6
    assert second.line_no == 20
    assert {e.uuid for e in first.events} | {e.uuid for e in second.events} == {
        e.uuid for e in read_events(path).events
    }


def test_bad_lines(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('not json\n[1, 2]\n{"type": "user", "message": {"content": "hi"}}\n')
    result = read_events(path)
    assert result.bad_lines == [1, 2]
    assert [e.kind for e in result.events] == [Kind.USER_PROMPT]


def test_multiple_blocks_in_one_line(tmp_path):
    path = tmp_path / "s.jsonl"
    record = {
        "type": "assistant",
        "uuid": "x",
        "message": {
            "content": [
                {"type": "text", "text": "a"},
                {"type": "tool_use", "id": "t", "name": "Write", "input": {"file_path": "/p/f.py"}},
            ]
        },
    }
    path.write_text(json.dumps(record) + "\n")
    events = read_events(path).events
    assert [(e.block, e.kind) for e in events] == [(0, Kind.ASSISTANT_TEXT), (1, Kind.TOOL_USE)]
