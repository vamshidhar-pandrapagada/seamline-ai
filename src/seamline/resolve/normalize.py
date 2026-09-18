"""Canonical keys for interface names, fields and types, so different spellings match.

order.created, OrderCreated, ORDER_CREATED, order-created  -> "order.created"
POST /orders/{id}, post /orders/:orderId                   -> "POST /orders/{}"
agents.v1.TaskService/Submit                                 -> "agents.v1.task.service.submit"
"""

from __future__ import annotations

import re

_HTTP = re.compile(r"^\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/\S*)\s*$", re.IGNORECASE)
_PATH_PARAM = re.compile(r"\{[^}/]*\}|:[A-Za-z_][A-Za-z0-9_]*|<[^>/]*>")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")

# Equivalent type spellings across languages and prose
_TYPE_ALIASES = {
    "int": "integer",
    "int32": "integer",
    "int64": "integer",
    "uint32": "integer",
    "uint64": "integer",
    "sint32": "integer",
    "sint64": "integer",
    "long": "integer",
    "i32": "integer",
    "i64": "integer",
    "u32": "integer",
    "u64": "integer",
    "integer": "integer",
    "float": "number",
    "double": "number",
    "decimal": "number",
    "number": "number",
    "f32": "number",
    "f64": "number",
    "numeric": "number",
    "real": "number",
    "str": "string",
    "string": "string",
    "text": "string",
    "varchar": "string",
    "bool": "boolean",
    "boolean": "boolean",
    "bytes": "bytes",
    "timestamp": "timestamp",
    "datetime": "timestamp",
}


def words(name: str) -> list[str]:
    """Split on separators and camelCase: 'amountCents' / 'amount_cents' -> ['amount', 'cents']."""
    return [w.lower() for w in _SEPARATORS.split(_CAMEL.sub(" ", name)) if w]


def interface_key(name: str) -> str:
    name = name.strip().strip("`")
    http = _HTTP.match(name)
    if http:
        method, path = http.groups()
        path = _PATH_PARAM.sub("{}", path.rstrip("/") or "/").lower()
        return f"{method.upper()} {path}"
    return ".".join(words(name))


_FIELD_PREFIXES = ("field", "fields", "param", "arg")


def field_key(name: str) -> str:
    """Field names compare by words: amount_cents == amountCents == AMOUNT_CENTS.

    A leading "field"/"param" word is dropped: the extractor sometimes writes field_amount
    for the field `amount`."""
    w = words(name)
    if len(w) > 1 and w[0] in _FIELD_PREFIXES:
        w = w[1:]
    return "_".join(w)


def type_key(value: str) -> str | None:
    """The type a detail value names, if it clearly names one ('int64', 'an integer')."""
    tokens = [w for w in re.split(r"[^a-z0-9]+", value.lower()) if w]
    types = {_TYPE_ALIASES[t] for t in tokens if t in _TYPE_ALIASES}
    return types.pop() if len(types) == 1 else None
