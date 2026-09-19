import pytest

from seamline.resolve.normalize import field_key, interface_key, type_key


@pytest.mark.parametrize(
    "a, b",
    [
        ("order.created", "OrderCreated"),
        ("order.created", "ORDER_CREATED"),
        ("order.created", "order-created"),
        ("order.created", "`order.created`"),
        ("POST /orders/{id}", "post /orders/:orderId"),
        ("GET /orders/<id>/", "GET /orders/{order_id}"),
        ("REDIS_URL", "redis_url"),
    ],
)
def test_same_interface(a, b):
    assert interface_key(a) == interface_key(b)


@pytest.mark.parametrize(
    "a, b",
    [
        ("order.created", "order.updated"),
        ("POST /orders", "GET /orders"),
        ("POST /orders/{id}", "POST /orders"),
    ],
)
def test_different_interfaces(a, b):
    assert interface_key(a) != interface_key(b)


def test_grpc_name():
    assert interface_key("agents.v1.TaskService/Submit") == "agents.v1.task.service.submit"


def test_field_key():
    assert field_key("amount_cents") == field_key("amountCents") == field_key("AMOUNT_CENTS")
    assert field_key("amount") != field_key("amount_cents")
    assert field_key("field_amount") == field_key("amount") == field_key("param_amount")
    assert field_key("field") == "field"  # a field actually called "field" stays


@pytest.mark.parametrize(
    "value, expected",
    [
        ("int64", "integer"),
        ("integer", "integer"),
        ("an integer, in cents", "integer"),
        ("decimal", "number"),
        ("float (dollars)", "number"),
        ("integer number of cents", "integer"),
        ("string", "string"),
        ("dollars", None),
        ("int or string", None),  # ambiguous
        ("OrderCreated", None),
    ],
)
def test_type_key(value, expected):
    assert type_key(value) == expected
