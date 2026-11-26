"""
Read-only graph queries (traversal and display) used by RepoKnowledgeGraph and the MCP server.
"""
import logging
from typing import Dict, List, Optional

import networkx as nx

from .builder import CONTAINS

logger = logging.getLogger(__name__)


def print_tree(kg, max_depth=None, start_node_id='root', level=0, prefix=""):
    """Print the repository tree ('contains' edges)."""
    if max_depth is not None and level > max_depth:
        return
    if start_node_id not in kg.graph:
        logger.warning(f"Start node '{start_node_id}' not found in graph.")
        return
    node_data = kg[start_node_id]
    symbols = {'file': "📄", 'chunk': "📝", 'root': "📁", 'directory': "📂"}
    node_symbol = symbols.get(node_data.node_type, "📦")
    if level == 0:
        print(f"{node_symbol} {node_data.name} ({node_data.node_type})")
    else:
        print(f"{prefix}└── {node_symbol} {node_data.name} ({node_data.node_type})")

    children = [child for child in kg.graph.successors(start_node_id)
                if kg.graph.edges[start_node_id, child].get('relation') == CONTAINS]
    for i, child_id in enumerate(children):
        new_prefix = prefix + ("    " if i == len(children) - 1 else "│   ")
        print_tree(kg, max_depth, start_node_id=child_id, level=level + 1, prefix=new_prefix)


def find_path(kg, source_id: str, target_id: str, max_depth: int = 5) -> dict:
    """Shortest (directed) path between two nodes."""
    g = kg.graph
    if source_id not in g:
        return {"error": f"Source node '{source_id}' not found.", "error_type": "node_not_found"}
    if target_id not in g:
        return {"error": f"Target node '{target_id}' not found.", "error_type": "node_not_found"}
    try:
        path = nx.shortest_path(g, source=source_id, target=target_id)
    except nx.NetworkXNoPath:
        return {"source_id": source_id, "target_id": target_id, "path": [], "length": -1,
                "text": f"No path found between '{source_id}' and '{target_id}'"}

    length = len(path) - 1
    if length > max_depth:
        return {"source_id": source_id, "target_id": target_id, "path": [], "length": length,
                "text": f"Path exists but exceeds max_depth of {max_depth} (actual length: {length})"}

    path_details = []
    for i, node_id in enumerate(path):
        node = g.nodes[node_id]['data']
        info = {"node_id": node_id, "name": getattr(node, 'name', 'Unknown'),
                "type": getattr(node, 'node_type', 'Unknown'), "step": i}
        if i < length:
            info["edge_to_next"] = g.edges[node_id, path[i + 1]].get('relation', 'Unknown')
        path_details.append(info)

    text = f"Path from '{source_id}' to '{target_id}' (length: {length}):\n\n"
    for i, info in enumerate(path_details):
        text += f"{i}. {info['name']} ({info['type']})\n   Node ID: {info['node_id']}\n"
        if 'edge_to_next' in info:
            text += f"   --[{info['edge_to_next']}]--> \n"
    return {"source_id": source_id, "target_id": target_id, "path": path_details,
            "length": length, "text": text}

def get_subgraph(kg, node_id: str, depth: int = 2, edge_types: Optional[List[str]] = None) -> dict:
    """Nodes and edges within `depth` hops of node_id (both directions), optionally filtered by relation."""
    g = kg.graph
    if node_id not in g:
        return {"error": f"Node '{node_id}' not found.", "error_type": "node_not_found"}

    def allowed(u, v):
        return edge_types is None or g.edges[u, v].get('relation') in edge_types

    frontier, all_nodes = {node_id}, {node_id}
    for _ in range(depth):
        next_level = set()
        for n in frontier:
            next_level.update(m for m in g.successors(n) if allowed(n, m))
            next_level.update(m for m in g.predecessors(n) if allowed(m, n))
        frontier = next_level - all_nodes
        all_nodes |= next_level

    subgraph = g.subgraph(all_nodes)
    nodes = [{"node_id": n, "name": getattr(subgraph.nodes[n]['data'], 'name', 'Unknown'),
              "type": getattr(subgraph.nodes[n]['data'], 'node_type', 'Unknown')} for n in subgraph.nodes()]
    edges = [{"source": s, "target": t, "relation": d.get('relation', 'Unknown')}
             for s, t, d in subgraph.edges(data=True) if allowed(s, t)]

    nodes_by_type: Dict[str, list] = {}
    for node in nodes:
        nodes_by_type.setdefault(node['type'], []).append(node)
    edge_by_relation: Dict[str, int] = {}
    for edge in edges:
        edge_by_relation[edge['relation']] = edge_by_relation.get(edge['relation'], 0) + 1

    text = f"Subgraph around '{node_id}' (depth: {depth}):\n"
    if edge_types:
        text += f"Edge types filter: {', '.join(edge_types)}\n"
    text += f"\nNodes: {len(nodes)}\nEdges: {len(edges)}\n\n"
    for node_type, type_nodes in nodes_by_type.items():
        text += f"{node_type} ({len(type_nodes)}):\n"
        for node in type_nodes[:5]:
            text += f"  - {node['name']} ({node['node_id']})\n"
        if len(type_nodes) > 5:
            text += f"  ... and {len(type_nodes) - 5} more\n"
        text += "\n"
    if edge_by_relation:
        text += "Edge types:\n" + "".join(f"  - {r}: {c}\n" for r, c in edge_by_relation.items())

    return {"center_node_id": node_id, "depth": depth, "edge_types_filter": edge_types,
            "node_count": len(nodes), "edge_count": len(edges), "nodes": nodes, "edges": edges,
            "nodes_by_type": nodes_by_type, "edge_by_relation": edge_by_relation, "text": text}
