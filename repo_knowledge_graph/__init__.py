"""
repo_knowledge_graph: knowledge graphs of code repositories, served over MCP.

    from repo_knowledge_graph import RepoKnowledgeGraph, KnowledgeGraphMCPServer
"""
from .knowledge_graph import RepoKnowledgeGraph
from .node import ChunkNode, CommitNode, DirectoryNode, EntityNode, FileNode, Node

__version__ = '0.3.0'


def __getattr__(name):
    # Imported lazily: fastmcp is only needed for the server
    if name == 'KnowledgeGraphMCPServer':
        from .mcp_server import KnowledgeGraphMCPServer
        return KnowledgeGraphMCPServer
    raise AttributeError(name)


__all__ = ['RepoKnowledgeGraph', 'KnowledgeGraphMCPServer', 'Node', 'DirectoryNode', 'FileNode',
           'ChunkNode', 'EntityNode', 'CommitNode', '__version__']
