"""A two-service shop project with one session each, and a fake model for it."""

from transcript_factory import prompt, reply, write_session

from seamline.config import parse_config

ORDERS_SAYS = "orders will emit order.created with amount_cents as an integer"
PAYMENTS_SAYS = "we read amount from order.created as a decimal"  # names no service


def fact(kind, quote, details, line, service=None):
    return {
        "kind": kind,
        "claim": quote,
        "interface": "order.created",
        "interface_kind": "event",
        "service": service,
        "details": [{"name": k, "value": v} for k, v in details.items()],
        "quote": quote,
        "line": line,
        "confidence": 0.9,
    }


def model(prompt_text):
    """A stand-in for Haiku: returns the fact matching whatever the excerpt contains."""
    facts = []
    if ORDERS_SAYS in prompt_text:
        facts.append(fact("provides", ORDERS_SAYS, {"amount_cents": "integer"}, 3))
    if PAYMENTS_SAYS in prompt_text:
        facts.append(fact("assumes", PAYMENTS_SAYS, {"amount": "decimal"}, 3))
    return {"facts": facts}


def edit(uuid, path):
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"t-{uuid}",
                    "name": "Edit",
                    "input": {"file_path": path},
                }
            ],
        },
    }


def make_shop(tmp_path, home):
    root = tmp_path / "shop"
    for s in ("orders", "payments"):
        (root / "services" / s).mkdir(parents=True)
    (root / "seamline.toml").write_text(
        'project = "shop"\n[services]\norders = "services/orders"\npayments = "services/payments"\n'
    )
    config = parse_config(
        {
            "project": "shop",
            "services": {"orders": "services/orders", "payments": "services/payments"},
        },
        root=root,
    )
    write_session(
        home,
        root / "services/orders",
        "orders-1",
        [prompt("p1", ORDERS_SAYS), reply("r1", "done")],
        started="2026-09-01T10:00:00Z",
    )
    # A session at the project root that edits payments files
    write_session(
        home,
        root,
        "root-1",
        [
            prompt("p2", PAYMENTS_SAYS),
            edit("e2", str(root / "services/payments/charge.py")),
            reply("r2", "ok"),
        ],
        started="2026-09-02T10:00:00Z",
    )
    return config
