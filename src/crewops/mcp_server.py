"""MCP server exposing the tool registry, acting as one team member.

uv run python -m crewops.mcp_server --user ananya.iyer
uv run python -m crewops.mcp_server --user meera.bhatt --http --port 8765
claude mcp add crewops -- uv run --directory <repo> python -m crewops.mcp_server --user ananya.iyer"""

from __future__ import annotations

import argparse
import os

from mcp.server.mcpserver import MCPServer

from crewops.agents import tools as registry
from crewops.agents.tools import Caller
from crewops.graph import Graphs, graphs

INSTRUCTIONS = """CrewOps crew control over a FalkorDB graph of a three-airline operation.
Use blast_radius or get_disruption to find empty seats, find_cover to rank legal cover, check_legality
for one person, delay_impact for delays, explain_decision and why_not to explain a decision.
Every result includes the Cypher queries that produced it under 'evidence'. Quote ids and INR costs
exactly; never compute legality yourself."""


def build(username: str, gs: Graphs | None = None) -> MCPServer:
    gs = gs or graphs()
    caller = Caller.load(gs, username)
    server = MCPServer(name="crewops", instructions=INSTRUCTIONS, version="0.1.0")
    for tool in registry.available(caller):
        server.add_tool(
            registry.mcp_function(gs, lambda: caller, tool),
            name=tool.name,
            description=tool.description,
        )
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve the CrewOps tools over MCP")
    parser.add_argument("--user", default=os.environ.get("CREWOPS_USER"), help="team member to act as")
    parser.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not args.user:
        parser.error("name the team member with --user or CREWOPS_USER (for example ananya.iyer)")
    server = build(args.user)
    if args.http:
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run()


if __name__ == "__main__":
    main()
