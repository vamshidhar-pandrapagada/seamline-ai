"""The MCP server (`seamline mcp`): Seamline's ledger as tools Claude can call.

Claude Code starts one server per session over stdio, from the project's `.mcp.json`. Every
tool opens its own ledger connection, so calls are independent. All tools are read-only
except `remember`. Startup facts (working folder, whether the session id is visible) go to
`.seamline/logs/mcp.log`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from seamline import paths
from seamline.config import INTEGRATION, Config
from seamline.ledger.db import open_ledger
from seamline.mapping import service_for
from seamline.mcp_server import freshness, tools

log = logging.getLogger("seamline.mcp")

INSTRUCTIONS = """\
Seamline is this project's shared memory across Claude Code sessions: what each service
provides and consumes, what other sessions assumed about those interfaces, decisions made,
and approaches that were tried and abandoned. Facts come from earlier sessions (with the
quote and session they came from) and from contract files; treat them as leads to verify,
not as ground truth.

Use it before you plan or change anything that crosses a service boundary:
- get_integration_context(services): before working on how services talk to each other
  (events, APIs, RPCs, shared config), to see both sides' claims and open mismatches.
- check_contract(interface): before changing or relying on one event/endpoint/message, to
  see every recorded claim about its fields.
- find_dead_ends(topic): before trying an approach, to see whether it was already tried and
  why it failed.
- search_history(query): when you need to know why something is the way it is.
- remember(fact): only when the user states or confirms a fact worth keeping for other
  sessions (a decision, a contract rule). Don't store your own guesses.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def build_server(config: Config, own_session: str | None = None, cwd: Path | None = None):
    """The server for one project. `own_session` (the calling Claude Code session, when
    known) is left out of catch-up; `cwd` gives `remember` its default service."""
    here = service_for(cwd, config) if cwd else None
    default_service = None if here in (None, INTEGRATION) else here
    server = MCPServer("seamline", instructions=INSTRUCTIONS)

    def answer(body: str, services: set[str] | None, pending) -> str:
        conn = open_ledger(config.root)
        try:
            return body + "\n\n" + freshness.describe(conn, services, pending)
        finally:
            conn.close()

    @server.tool(annotations=READ_ONLY)
    def get_integration_context(services: list[str]) -> str:
        """How these services fit together: every interface two of them share, what each
        side provides, consumes or assumes about it (with sources), and open contract
        mismatches first. Pass service names from the project, e.g. ["orders", "payments"];
        an empty list means all services. Waits up to 30 s for other sessions' latest lines
        to be read first."""
        wanted = set(services) or set(config.services)
        conn = open_ledger(config.root)
        try:
            pending = freshness.catch_up(conn, config, wanted, own_session)
            body = tools.integration_context(conn, config, list(wanted))
        finally:
            conn.close()
        return answer(body, wanted, pending)

    @server.tool(annotations=READ_ONLY)
    def check_contract(interface: str) -> str:
        """Everything recorded about one interface (an event, endpoint, RPC, message, topic,
        table, env var): each service's claims about its fields with quotes and dates, open
        mismatches, and superseded history. Spelling is forgiving (order.created,
        OrderCreated and ORDER_CREATED match). Waits up to 30 s for other sessions' latest
        lines to be read first."""
        conn = open_ledger(config.root)
        try:
            pending = freshness.catch_up(conn, config, None, own_session)
            body = tools.contract(conn, config, interface)
        finally:
            conn.close()
        return answer(body, None, pending)

    @server.tool(annotations=READ_ONLY)
    def find_dead_ends(topic: str) -> str:
        """Approaches already tried in this project and abandoned, matching a topic (e.g.
        "retry", "idempotency", "grpc streaming"), with why they failed and where that was
        said. An empty topic lists all of them."""
        conn = open_ledger(config.root)
        try:
            return tools.dead_ends(conn, topic)
        finally:
            conn.close()

    @server.tool(annotations=READ_ONLY)
    def search_history(query: str) -> str:
        """Decisions made in earlier sessions matching a query, current and superseded, with
        their sources: use it to find out why something is the way it is."""
        conn = open_ledger(config.root)
        try:
            return tools.history(conn, query)
        finally:
            conn.close()

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def remember(
        fact: str,
        kind: str = "decision",
        service: str | None = None,
        interface: str | None = None,
    ) -> str:
        """Store a fact the user stated or confirmed, so other sessions see it. `kind` is
        decision (default), dead_end, provides, consumes or assumes; `service` defaults to
        the folder this session runs in; `interface` names the event/endpoint it concerns,
        if any. Only for the user's own statements, never for guesses."""
        conn = open_ledger(config.root)
        try:
            return tools.remember(
                conn,
                config,
                fact,
                kind=kind,
                service=service or default_service,
                interface=interface,
                session_id=own_session,
            )
        finally:
            conn.close()

    return server


def run(config: Config) -> None:
    log_path = paths.logs_dir(config.root) / "mcp.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_path, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    own_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    cwd = Path(os.getcwd())
    log.info(
        "start: cwd=%s service=%s session_id=%s",
        cwd,
        service_for(cwd, config),
        (own_session or "unknown")[:8],
    )
    build_server(config, own_session, cwd).run("stdio")
