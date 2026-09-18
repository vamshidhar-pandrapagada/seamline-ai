"""`seamline scan`: read the [contracts] files into code facts (origin = code).

Code facts are ground truth for drift. Each one has a `source_ref` ("proto/orders.proto#
OrderCreated") so a rescan can tell unchanged facts (kept), changed ones (the old one is
superseded) and removed ones (marked stale).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from seamline.config import INTEGRATION, Config
from seamline.contracts.compose import parse_compose_file
from seamline.contracts.proto import parse_proto_file
from seamline.detect import SKIP_DIRS
from seamline.ledger import queries as q
from seamline.mapping import service_for
from seamline.resolve.matcher import (
    add_spelling,
    find_interface,
    interface_id_for,
    merge_interfaces,
    same_details,
)

_COMPOSE_RE = re.compile(r"^(docker-)?compose([.-][\w.-]+)?\.ya?ml$")


@dataclass
class CodeFact:
    kind: str  # provides | consumes
    service: str | None
    interface: str
    interface_kind: str | None
    claim: str
    details: list[dict]
    file: str  # Relative to the project root
    line: int
    source_ref: str
    quote: str
    also_known_as: tuple[str, ...] = ()  # Other spellings: the package-qualified name


@dataclass
class ScanResult:
    files: list[str] = field(default_factory=list)
    added: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0


def contract_files(config: Config) -> list[Path]:
    files: set[Path] = set()
    for rel in config.contracts:
        p = (config.root / rel).resolve()
        if p.is_file():
            files.add(p)
        elif p.is_dir():
            for f in p.rglob("*"):
                if (
                    f.is_file()
                    and not (set(f.relative_to(p).parts) & SKIP_DIRS)
                    and (f.suffix == ".proto" or _COMPOSE_RE.match(f.name))
                ):
                    files.add(f)
    return sorted(files)


def read_code_facts(config: Config) -> list[CodeFact]:
    facts: list[CodeFact] = []
    for path in contract_files(config):
        if path.suffix == ".proto":
            facts += _proto_facts(path, config)
        elif _COMPOSE_RE.match(path.name):
            facts += _compose_facts(path, config)
    return facts


def scan(conn: sqlite3.Connection, config: Config) -> ScanResult:
    found = read_code_facts(config)
    result = ScanResult(files=sorted({f.file for f in found}))
    previous = {f.source_ref: f for f in q.list_facts(conn, origin="code") if f.source_ref}
    seen: set[str] = set()
    for cf in found:
        seen.add(cf.source_ref)
        interface_id = _interface_for(conn, cf)
        old = previous.get(cf.source_ref)
        if (
            old
            and old.interface_id == interface_id
            and old.kind == cf.kind
            and old.service == cf.service
            and same_details(old.details, cf.details)
        ):
            result.unchanged += 1
            continue
        fact_id = q.insert_fact(
            conn,
            kind=cf.kind,
            origin="code",
            service=cf.service,
            attributed_by="code",
            interface_id=interface_id,
            claim=cf.claim,
            details=cf.details,
            confidence=1.0,
            source_ref=cf.source_ref,
        )
        q.add_evidence(conn, fact_id, quote=cf.quote, file_path=cf.file, line_no=cf.line)
        q.add_files(conn, fact_id, [cf.file])
        q.log_change(conn, "fact_added", [cf.service], fact_id=fact_id)
        if old:
            q.set_status(conn, old.id, "superseded")
            q.link_supersedes(conn, fact_id, old.id)
            q.log_change(conn, "fact_superseded", [old.service], fact_id=old.id)
            result.changed += 1
        else:
            result.added += 1
    for ref, old in previous.items():
        if ref not in seen:
            q.mark_stale(conn, old.id, "removed", f"no longer in {old.source_ref}")
            q.log_change(conn, "fact_stale", [old.service], fact_id=old.id)
            result.removed += 1
    return result


def _interface_for(conn: sqlite3.Connection, cf: CodeFact) -> int:
    """The code fact's interface, linked to every spelling sessions may use. If sessions
    already created a separate interface under another spelling, merge it in."""
    interface_id = interface_id_for(conn, cf.interface, cf.interface_kind)
    for name in cf.also_known_as:
        other = find_interface(conn, name)
        if other is not None and other != interface_id:
            merge_interfaces(conn, interface_id, other)
        add_spelling(conn, name, interface_id)
    return interface_id


def _owner(path: Path, config: Config) -> str | None:
    service = service_for(path.parent, config)
    return None if service in (None, INTEGRATION) else service


def _proto_facts(path: Path, config: Config) -> list[CodeFact]:
    parsed = parse_proto_file(path)
    rel = str(path.relative_to(config.root))
    owner = _owner(path, config)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    pkg = f"{parsed.package}." if parsed.package else ""
    out = []
    for msg in parsed.messages:
        fields = ", ".join(f"{n} ({t})" for n, t in msg.fields) or "no fields"
        out.append(
            CodeFact(
                kind="provides",
                service=owner,
                interface=msg.name,
                interface_kind=None,
                claim=f"Proto message {pkg}{msg.name} has fields: {fields}.",
                details=[{"name": n, "value": t} for n, t in msg.fields],
                file=rel,
                line=msg.line,
                source_ref=f"{rel}#{msg.name}",
                quote=_line(lines, msg.line),
                also_known_as=(f"{pkg}{msg.name}",) if pkg else (),
            )
        )
    for rpc in parsed.rpcs:
        name = f"{pkg}{rpc.service}/{rpc.method}"
        out.append(
            CodeFact(
                kind="provides",
                service=owner,
                interface=name,
                interface_kind="grpc",
                claim=f"gRPC {name} takes {rpc.request} and returns {rpc.response}.",
                details=[
                    {"name": "request", "value": rpc.request},
                    {"name": "response", "value": rpc.response},
                ],
                file=rel,
                line=rpc.line,
                source_ref=f"{rel}#{rpc.service}/{rpc.method}",
                quote=_line(lines, rpc.line),
            )
        )
    return out


def _compose_facts(path: Path, config: Config) -> list[CodeFact]:
    rel = str(path.relative_to(config.root))
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for svc in parse_compose_file(path):
        owner = _compose_owner(svc.name, svc.build_context, path, config)
        quote = _line(lines, svc.line)
        for port in svc.ports:
            out.append(
                CodeFact(
                    kind="provides",
                    service=owner,
                    interface=f"{svc.name}:{port}",
                    interface_kind=None,
                    claim=f"Compose service {svc.name} listens on container port {port}.",
                    details=[{"name": "port", "value": port}],
                    file=rel,
                    line=svc.line,
                    source_ref=f"{rel}#{svc.name}/port/{port}",
                    quote=quote,
                )
            )
        for var in svc.env:
            out.append(
                CodeFact(
                    kind="consumes",
                    service=owner,
                    interface=var,
                    interface_kind="env",
                    claim=f"Compose service {svc.name} is configured with env var {var}.",
                    details=[],
                    file=rel,
                    line=svc.line,
                    source_ref=f"{rel}#{svc.name}/env/{var}",
                    quote=quote,
                )
            )
    return out


def _compose_owner(name: str, context: str | None, compose: Path, config: Config) -> str | None:
    """A compose service belongs to the Seamline service whose folder holds its build
    context, else the service with the same name, else the service whose folder has that
    name (compose `agent-core` -> service `core` at agent-core/)."""
    if context:
        owner = service_for((compose.parent / context).resolve(), config)
        if owner not in (None, INTEGRATION):
            return owner
    if name in config.services:
        return name
    by_folder = [s for s, rel in config.services.items() if Path(rel).name == name]
    return by_folder[0] if len(by_folder) == 1 else None


def _line(lines: list[str], n: int) -> str:
    return lines[n - 1].strip() if 0 < n <= len(lines) else ""
