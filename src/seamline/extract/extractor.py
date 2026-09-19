"""Session → labeled events → excerpts → model → validated, quote-verified facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources

from pydantic import ValidationError

from seamline.config import Config
from seamline.extract.chunker import DEFAULT_BUDGET_TOKENS, Chunk, build_chunks
from seamline.extract.providers import LLMProvider, ProviderError, ProviderStop
from seamline.extract.schema import FACT_JSON_SCHEMA, ExtractedFact, ExtractionOutput
from seamline.extract.verify import verify
from seamline.transcripts.classify import classify
from seamline.transcripts.models import Event

PROMPT_VERSION = "extract_facts.md@7"


def system_prompt() -> str:
    return resources.files("seamline.extract").joinpath("prompts/extract_facts.md").read_text()


@dataclass
class Fact:
    """A fact that passed validation and quote verification, with where it came from."""

    fact: ExtractedFact
    session_id: str
    line: int
    timestamp: str | None
    cwd: str | None


@dataclass
class Rejected:
    fact: ExtractedFact
    reason: str


@dataclass
class ExtractionReport:
    session_id: str
    facts: list[Fact] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # One per failed excerpt
    stopped: bool = False  # A call failed in a way every later call would too (key, cap)
    chunks: int = 0
    chunks_done: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    prompt_version: str = PROMPT_VERSION


def extract_events(
    events: list[Event],
    config: Config,
    provider: LLMProvider,
    *,
    session_id: str,
    inherited: set[str] | frozenset[str] = frozenset(),
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    max_chunks: int | None = None,
) -> ExtractionReport:
    labeled = [(e, classify(e, inherited)) for e in events]
    chunks = build_chunks(labeled, root=config.root, budget_tokens=budget_tokens)
    report = ExtractionReport(session_id=session_id, chunks=len(chunks))
    system = system_prompt()
    for chunk in chunks[:max_chunks] if max_chunks else chunks:
        try:
            completion = provider.complete(system, _user_prompt(config, chunk), FACT_JSON_SCHEMA)
        except ProviderStop as e:
            report.errors.append(str(e))
            report.stopped = True
            break
        except ProviderError as e:
            report.errors.append(f"lines {chunk.lines[0]}-{chunk.lines[1]}: {e}")
            continue
        report.chunks_done += 1
        report.input_tokens += completion.input_tokens
        report.output_tokens += completion.output_tokens
        try:
            output = ExtractionOutput.model_validate(completion.data)
        except ValidationError as e:
            first, last = chunk.lines
            report.errors.append(f"lines {first}-{last}: invalid output ({e.error_count()} errors)")
            continue
        _accept(output.facts, chunk, config, report)
    return report


def _accept(facts: list[ExtractedFact], chunk: Chunk, config: Config, report: ExtractionReport):
    by_line = {e.line_no: e for e in chunk.events}
    for fact in facts:
        verdict = verify(fact, chunk)
        if not verdict.ok:
            report.rejected.append(Rejected(fact, verdict.reason))
            continue
        if fact.service is not None and fact.service not in config.services:
            fact = fact.model_copy(update={"service": None})  # Only listed services count
        source = by_line[verdict.line]
        report.facts.append(
            Fact(
                fact=fact,
                session_id=report.session_id,
                line=verdict.line,
                timestamp=source.timestamp,
                cwd=source.cwd,
            )
        )


def _user_prompt(config: Config, chunk: Chunk) -> str:
    services = "\n".join(f"- {name}: {path}/" for name, path in config.services.items())
    return (
        f"Project: {config.project}\n"
        f"Services (name: folder):\n{services or '- (none listed; everything is one scope)'}\n\n"
        f"Session excerpt:\n\n{chunk.text}"
    )
