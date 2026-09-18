from dataclasses import replace
from pathlib import Path

from seamline.transcripts.classify import Label, classify
from seamline.transcripts.models import Event, Kind
from seamline.transcripts.reader import read_events

FIXTURE = Path(__file__).parent / "fixtures" / "transcripts" / "basic.jsonl"


def labels():
    return {e.uuid: classify(e) for e in read_events(FIXTURE).events if e.uuid}


def test_fixture_labels():
    got = labels()
    assert got["u-prompt"] is Label.KEEP
    assert got["u-text"] is Label.KEEP
    assert got["u-secret"] is Label.KEEP
    assert got["u-edit"] is Label.ANCHOR
    assert got["u-env"] is Label.ANCHOR
    assert got["u-bash"] is Label.EVIDENCE
    assert got["u-edit-res"] is Label.EVIDENCE
    assert got["u-summary"] is Label.EVIDENCE
    for skipped in ("u-think", "u-interrupt", "u-meta", "u-cmd", "u-boundary", "u-att"):
        assert got[skipped] is Label.SKIP, skipped


def test_inherited_is_skipped():
    event = Event(line_no=1, block=0, kind=Kind.USER_PROMPT, text="hi", uuid="a")
    assert classify(event) is Label.KEEP
    assert classify(event, {"a"}) is Label.SKIP


def test_sidechain():
    prompt = Event(line_no=1, block=0, kind=Kind.USER_PROMPT, text="task", is_sidechain=True)
    edit = Event(
        line_no=2,
        block=0,
        kind=Kind.TOOL_USE,
        tool_name="Write",
        tool_input={"file_path": "/a.py"},
        is_sidechain=True,
    )
    assert classify(prompt) is Label.SKIP
    assert classify(edit) is Label.ANCHOR


def test_blank_text_skipped():
    event = Event(line_no=1, block=0, kind=Kind.ASSISTANT_TEXT, text="  \n")
    assert classify(event) is Label.SKIP
    assert classify(replace(event, text="ok")) is Label.KEEP
