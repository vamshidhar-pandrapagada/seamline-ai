
import pytest

from seamline.config import parse_config
from seamline.contracts.compose import parse_compose
from seamline.contracts.proto import parse_proto
from seamline.contracts.scan import contract_files, scan
from seamline.ledger import queries as q
from seamline.ledger.db import connect

PROTO = """
syntax = "proto3";
package shop.v1;
/* An order
   was placed */
message OrderCreated {
  string order_id = 1;
  int64 amount_cents = 2; // cents
  repeated LineItem items = 3;
  map<string, string> labels = 4;
  message LineItem { string sku = 1; int32 qty = 2; }
  enum Status { NEW = 0; PAID = 1; }
  oneof payer { string card_token = 5; string wallet_id = 6; }
  reserved 9;
  option deprecated = true;
}
service Orders {
  rpc Create (CreateOrderRequest) returns (OrderCreated);
  rpc Watch (stream WatchRequest) returns (stream OrderCreated) {}
}
"""

COMPOSE = """
services:
  orders:
    build: ./services/orders
    ports: ["8080:8000", "9090:9000/tcp"]
    environment:
      DATABASE_URL: postgres://app:secret@db/orders
      REDIS_URL: redis://cache
    depends_on: [db]
  payments:
    build:
      context: services/payments
    ports:
      - target: 7000
        published: 7000
    environment:
      - STRIPE_KEY=sk_live_abc
  db:
    image: postgres
"""


def test_parse_proto():
    p = parse_proto(PROTO)
    assert p.package == "shop.v1"
    by_name = {m.name: m for m in p.messages}
    assert by_name["OrderCreated"].line == 6
    assert by_name["OrderCreated"].fields == [
        ("order_id", "string"),
        ("amount_cents", "int64"),
        ("items", "LineItem"),
        ("labels", "map<string,string>"),
        ("card_token", "string"),
        ("wallet_id", "string"),
    ]
    assert by_name["OrderCreated.LineItem"].fields == [("sku", "string"), ("qty", "int32")]
    assert [(r.method, r.request, r.response) for r in p.rpcs] == [
        ("Create", "CreateOrderRequest", "OrderCreated"),
        ("Watch", "WatchRequest", "OrderCreated"),
    ]


def test_parse_compose_never_keeps_env_values():
    services = {s.name: s for s in parse_compose(COMPOSE)}
    assert services["orders"].ports == ["8000", "9000"]
    assert services["orders"].env == ["DATABASE_URL", "REDIS_URL"]
    assert services["orders"].depends_on == ["db"]
    assert services["orders"].line == 3
    assert services["payments"].build_context == "services/payments"
    assert services["payments"].ports == ["7000"]
    assert services["payments"].env == ["STRIPE_KEY"]
    assert "secret" not in repr(services) and "sk_live" not in repr(services)


def test_parse_compose_tolerates_garbage():
    assert parse_compose("::: not yaml [") == []
    assert parse_compose("version: '3'") == []


@pytest.fixture
def project(tmp_path):
    (tmp_path / "proto").mkdir()
    (tmp_path / "proto" / "shop.proto").write_text(PROTO)
    (tmp_path / "docker-compose.yml").write_text(COMPOSE)
    for s in ("orders", "payments"):
        (tmp_path / "services" / s).mkdir(parents=True)
    return parse_config(
        {
            "project": "shop",
            "services": {"orders": "services/orders", "payments": "services/payments"},
            "contracts": {"paths": ["proto", "docker-compose.yml"]},
        },
        root=tmp_path,
    )


def test_scan_then_rescan(project):
    conn = connect(":memory:")
    assert [p.name for p in contract_files(project)] == ["docker-compose.yml", "shop.proto"]
    first = scan(conn, project)
    assert first.added > 0 and first.changed == first.removed == 0
    facts = q.list_facts(conn, origin="code")
    order = next(f for f in facts if f.interface == "OrderCreated")
    assert order.service is None  # shared proto folder: integration scope
    assert {"name": "amount_cents", "value": "int64"} in order.details
    ports = {f.interface: f.service for f in facts if ":" in (f.interface or "")}
    assert ports == {"orders:8000": "orders", "orders:9000": "orders", "payments:7000": "payments"}
    env = {f.interface: f.service for f in facts if f.kind == "consumes"}
    assert env == {"DATABASE_URL": "orders", "REDIS_URL": "orders", "STRIPE_KEY": "payments"}

    assert scan(conn, project).unchanged == first.added  # nothing changed

    proto = project.root / "proto" / "shop.proto"
    proto.write_text(
        PROTO.replace("int64 amount_cents = 2;", "string amount_cents = 2;").replace(
            "  rpc Watch", "  // rpc Watch"
        )
    )
    third = scan(conn, project)
    assert (third.changed, third.removed) == (1, 1)
    old = q.get_fact(conn, order.id)
    assert old.status == "superseded"
    new = q.get_fact(conn, q.superseded_by(conn, order.id))
    assert {"name": "amount_cents", "value": "string"} in new.details
    assert any(
        f.status == "stale" for f in q.list_facts(conn, origin="code", include_inactive=True)
    )


def test_compose_owner_by_folder_name(tmp_path):
    (tmp_path / "agent-core").mkdir()
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "docker-compose.yml").write_text(
        "services:\n  agent-core:\n    build:\n      context: ..\n    ports: ['50051:50051']\n"
    )
    config = parse_config(
        {
            "project": "h",
            "services": {"core": "agent-core"},
            "contracts": {"paths": ["docker/docker-compose.yml"]},
        },
        root=tmp_path,
    )
    conn = connect(":memory:")
    scan(conn, config)
    (fact,) = q.list_facts(conn, origin="code")
    assert fact.service == "core"


def test_session_and_proto_names_share_one_interface(project):
    from seamline.resolve.matcher import interface_id_for

    conn = connect(":memory:")
    # A session names the message by its package-qualified name before any scan
    interface_id_for(conn, "shop.v1.OrderCreated", None)
    scan(conn, project)
    code = next(f for f in q.list_facts(conn, origin="code") if f.interface == "OrderCreated")
    assert interface_id_for(conn, "shop.v1.OrderCreated", None) == code.interface_id
    assert interface_id_for(conn, "OrderCreated", None) == code.interface_id
    sql = "SELECT interface_id FROM interface_aliases WHERE alias IN (?, ?)"
    ids = {r[0] for r in conn.execute(sql, ("shop.v1.OrderCreated", "OrderCreated"))}
    assert ids == {code.interface_id}  # one interface left for the message
