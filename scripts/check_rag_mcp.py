"""Check a running RAG MCP server through MyAgent's normal tool execution chain."""
import argparse
import json
import os

from myagent.composition import build_default_components
from myagent.mcp import StreamableHTTPMCPServerConfig


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/mcp")
    parser.add_argument("--question", help="Run one actual RAG query; increments question counters")
    parser.add_argument("--knowledge-base-id", type=int, action="append", default=[])
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if args.question and not args.knowledge_base_id:
        parser.error("--question requires at least one --knowledge-base-id")
    token = os.getenv("RAG_MCP_API_KEY")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    config = StreamableHTTPMCPServerConfig(
        "rag", args.url, call_timeout_seconds=args.timeout, headers=headers,
    )
    # Running this diagnostic explicitly authorizes only the calls below.
    components = build_default_components(
        mcp_servers=[config], approval_callback=lambda _: True,
        allowed_tools={"mcp__rag__list_knowledge_bases", "mcp__rag__ask_knowledge_base"},
    )
    try:
        tool = "mcp__rag__list_knowledge_bases"
        arguments = {}
        if args.question:
            tool = "mcp__rag__ask_knowledge_base"
            arguments = {"question": args.question, "knowledge_base_ids": args.knowledge_base_id}
        result = components.tool_registry.execute(tool, json.dumps(arguments, ensure_ascii=False))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    finally:
        components.close()


if __name__ == "__main__":
    raise SystemExit(main())
