"""
Walk through the knowledge-graph MCP tools the way an agent would chain them.

Start a server first, then run this client against it:

    python -m repo_knowledge_graph serve --transport http --port 4000 --repo-path /path/to/repo
    python examples/mcp_client_demo.py [search query] [entity name]

MCP_SERVER_URL sets the server address (default http://localhost:4000).
"""
import asyncio
import json
import os
import sys

from fastmcp import Client


def show(title: str, result) -> dict:
    """Print a tool result (its text summary when it has one) and return its data."""
    data = result.data if isinstance(result.data, dict) else {}
    print(f"\n=== {title}")
    if result.is_error:
        print(f"error: {result.content[0].text if result.content else 'unknown error'}")
    elif data.get("text"):
        print(data["text"].strip())
    else:
        print(json.dumps(data or result.data, indent=2, default=str)[:1500])
    return data


async def main(query: str, entity: str | None) -> None:
    url = os.getenv("MCP_SERVER_URL", "http://localhost:4000").rstrip("/") + "/mcp"
    async with Client(url) as client:
        # 1. Discovery: what the agent sees before calling anything
        tools = await client.list_tools()
        print(f"Connected to {url}: {len(tools)} tools")
        stats = show("get_graph_stats", await client.call_tool("get_graph_stats", {}))
        print(json.dumps(stats.get("node_types", {})))

        # 2. Orientation: the layout of the repository
        show("print_tree (depth 2)", await client.call_tool("print_tree", {"max_depth": 2}))

        # 3. Search, then open the best match
        found = show(f"search_nodes {query!r}",
                     await client.call_tool("search_nodes", {"query": query, "limit": 3}))
        results = found.get("results") or []
        if results:
            first = results[0]["id"]
            show(f"get_node_info {first}", await client.call_tool("get_node_info", {"node_id": first}))

        # 4. Navigate an entity: where it is declared and who uses it
        if entity is None:
            listing = await client.call_tool(
                "list_all_entities", {"entity_type": "class", "declared_only": True, "limit": 1})
            entities = (listing.data or {}).get("entities") or []
            entity = entities[0]["name"] if entities else None
        if entity:
            show(f"go_to_definition {entity}",
                 await client.call_tool("go_to_definition", {"entity_name": entity, "limit": 2}))
            show(f"find_usages {entity}",
                 await client.call_tool("find_usages", {"entity_name": entity, "limit": 5}))

        # 5. Framework layer (Java / Spring Boot, web routes): from an endpoint down to the code it runs
        if stats.get("node_types", {}).get("endpoint"):
            endpoints = show("list_endpoints", await client.call_tool("list_endpoints", {"limit": 5}))
            items = endpoints.get("endpoints") or []
            if items:
                start = items[0]["id"]
                show(f"trace_flow {start}",
                     await client.call_tool("trace_flow", {"start": start, "max_depth": 4, "limit": 10}))
            show("list_findings (high severity)",
                 await client.call_tool("list_findings", {"severity": "high", "limit": 5}))


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(main(args[0] if args else "main", args[1] if len(args) > 1 else None))
