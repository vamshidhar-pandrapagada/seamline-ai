"""The facts extraction produces: a closed vocabulary, validated with Pydantic.

The JSON schema sent to the model is written by hand rather than generated, so it stays
within what structured outputs accept: every property required (nullable where optional),
no free-form maps, `additionalProperties: false` everywhere.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FactKind = Literal["provides", "consumes", "assumes", "decision", "dead_end"]
InterfaceKind = Literal["grpc", "http", "event", "topic", "table", "env", "config", "cli"]

FACT_KINDS: tuple[str, ...] = FactKind.__args__
INTERFACE_KINDS: tuple[str, ...] = InterfaceKind.__args__


class Detail(BaseModel):
    """One specific the claim depends on: a field and its type, a unit, a port, a topic."""

    model_config = ConfigDict(extra="forbid")
    name: str
    value: str


class ExtractedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: FactKind
    claim: str = Field(min_length=1)  # One self-contained sentence
    interface: str | None  # Literal name as written: order.created, POST /charge, REDIS_URL
    interface_kind: InterfaceKind | None
    service: str | None  # Only when the text explicitly names a listed service
    details: list[Detail]
    quote: str = Field(min_length=1)  # Verbatim from one USER/CLAUDE line
    line: int  # The [L n] tag the quote came from
    confidence: float = Field(ge=0, le=1)


class ExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facts: list[ExtractedFact]


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


FACT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(FACT_KINDS)},
                    "claim": {"type": "string"},
                    "interface": _nullable({"type": "string"}),
                    "interface_kind": _nullable({"type": "string", "enum": list(INTERFACE_KINDS)}),
                    "service": _nullable({"type": "string"}),
                    "details": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["name", "value"],
                            "additionalProperties": False,
                        },
                    },
                    "quote": {"type": "string"},
                    "line": {"type": "integer"},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "kind",
                    "claim",
                    "interface",
                    "interface_kind",
                    "service",
                    "details",
                    "quote",
                    "line",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}
