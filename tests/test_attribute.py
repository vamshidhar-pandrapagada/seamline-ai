from pathlib import Path

from seamline.config import parse_config
from seamline.resolve.attribute import attribute, touched_files, turn_of
from seamline.transcripts.models import Event, Kind

ROOT = Path("/work/shop")


def config():
    return parse_config(
        {
            "project": "shop",
            "services": {"orders": "services/orders", "payments": "services/payments"},
        },
        root=ROOT,
    )


def prompt(line):
    return Event(line_no=line, block=0, kind=Kind.USER_PROMPT, text="q", cwd=str(ROOT))


def reply(line):
    return Event(line_no=line, block=0, kind=Kind.ASSISTANT_TEXT, text="a", cwd=str(ROOT))


def edit(line, path, cwd=ROOT):
    return Event(
        line_no=line,
        block=0,
        kind=Kind.TOOL_USE,
        tool_name="Edit",
        tool_input={"file_path": path},
        cwd=str(cwd),
    )


def bash(line, command, cwd=ROOT):
    return Event(
        line_no=line,
        block=0,
        kind=Kind.TOOL_USE,
        tool_name="Bash",
        tool_input={"command": command},
        cwd=str(cwd),
    )


def test_root_session_fact_lands_on_touched_service():
    events = [
        prompt(1),
        edit(2, "/work/shop/services/orders/events.py"),
        reply(3),
        prompt(10),
        bash(11, "pytest services/payments/tests -q"),
        reply(12),
    ]
    a = attribute(None, 3, events, config(), "integration")
    assert (a.service, a.by) == ("orders", "files")
    assert a.files == ("services/orders/events.py",)
    b = attribute(None, 12, events, config(), "integration")
    assert (b.service, b.by) == ("payments", "files")


def test_explicit_name_beats_files():
    events = [prompt(1), edit(2, "/work/shop/services/orders/events.py"), reply(3)]
    a = attribute("payments", 3, events, config(), "orders")
    assert (a.service, a.by) == ("payments", "name")


def test_unknown_name_falls_through():
    events = [prompt(1), reply(2)]
    assert attribute("billing", 2, events, config(), "orders").by == "folder"


def test_split_vote_falls_back_to_folder_then_none():
    events = [
        prompt(1),
        edit(2, "/work/shop/services/orders/a.py"),
        edit(3, "/work/shop/services/payments/b.py"),
        reply(4),
    ]
    assert attribute(None, 4, events, config(), "orders").by == "folder"
    a = attribute(None, 4, events, config(), "integration")
    assert (a.service, a.by) == (None, "none")


def test_turn_boundaries():
    events = [prompt(1), reply(2), prompt(5), reply(6), reply(7)]
    assert [e.line_no for e in turn_of(6, events)] == [5, 6, 7]
    assert [e.line_no for e in turn_of(2, events)] == [1, 2]


def test_bash_paths_relative_to_cwd_and_outside_root_ignored():
    # Known limitation: `cd` inside a command isn't followed, so "events.py" after
    # `cd services/orders` resolves against the event's cwd (the root).
    events = [
        bash(1, "cd services/orders && cat events.py", cwd=ROOT),
        bash(2, "cat ../payments/api.py", cwd=ROOT / "services/orders"),
        bash(3, "cat /etc/hosts; curl https://example.com/a/b"),
        bash(4, "python - <<'EOF'\nprint(1)\nEOF"),
    ]
    got = {str(p) for p in touched_files(events, config())}
    assert got == {
        "/work/shop/services/orders",
        "/work/shop/events.py",
        "/work/shop/services/payments/api.py",
    }


def test_long_autonomous_turn_uses_the_step_around_each_statement():
    # One prompt, then work across services: vote per step, not over the whole turn
    events = [
        prompt(1),
        edit(2, "/work/shop/services/orders/a.py"),
        edit(3, "/work/shop/services/orders/b.py"),
        reply(4),  # "orders now emits …"
        edit(5, "/work/shop/services/payments/c.py"),
        reply(6),  # "payments now reads …"
        edit(7, "/work/shop/services/orders/d.py"),
    ]
    assert attribute(None, 4, events, config(), "integration").service == "orders"
    assert attribute(None, 6, events, config(), "integration").service == "payments"


def test_mixed_step_falls_back_to_the_turn():
    events = [
        prompt(1),
        edit(2, "/work/shop/services/orders/a.py"),
        edit(3, "/work/shop/services/orders/b.py"),
        edit(4, "/work/shop/services/orders/c.py"),
        reply(5),
        edit(6, "/work/shop/services/payments/x.py"),
        edit(7, "/work/shop/services/orders/y.py"),
        reply(8),
    ]
    # Before L8: 1 payments + 1 orders (mixed); nothing after; the turn: orders majority
    a = attribute(None, 8, events, config(), "integration")
    assert (a.service, a.by) == ("orders", "files")


def test_announcement_uses_the_work_that_follows():
    events = [
        prompt(1),
        reply(2),  # "Now making the edits in payments"
        edit(3, "/work/shop/services/payments/a.py"),
        edit(4, "/work/shop/services/payments/b.py"),
        reply(5),
    ]
    assert attribute(None, 2, events, config(), "integration").service == "payments"


def test_fact_text_mentioning_files_beats_nearby_work():
    events = [
        prompt(1),
        edit(2, "/work/shop/services/orders/events.py"),
        edit(3, "/work/shop/services/payments/charge.py"),
        edit(4, "/work/shop/services/orders/models.py"),
        reply(5),  # a final summary covering both services
    ]
    text = "charge.py now reads amount_cents from the event"
    a = attribute(None, 5, events, config(), "integration", text)
    assert (a.service, a.by) == ("payments", "files")
    assert a.files == ("services/payments/charge.py",)


def test_fact_text_naming_a_service_or_folder():
    events = [prompt(1), edit(2, "/work/shop/services/orders/a.py"), reply(3)]
    a = attribute(None, 3, events, config(), "integration", "payments reads amount as a decimal")
    assert (a.service, a.by) == ("payments", "name")
    # A tie between two named services decides nothing: fall through to files
    b = attribute(None, 3, events, config(), "integration", "orders and payments agree")
    assert (b.service, b.by) == ("orders", "files")


def test_mentioned_bare_name_must_be_unambiguous():
    from seamline.resolve.attribute import mentioned_files

    touched = [
        Path("/work/shop/services/orders/client.py"),
        Path("/work/shop/services/payments/client.py"),
    ]
    assert mentioned_files("client.py returns model", touched, config()) == []
