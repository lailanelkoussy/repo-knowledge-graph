"""
Search index over code chunks, with two interchangeable backends:

* LanceDB (default): embedded, no service to run. Native BM25 full-text index + vectors.
* Weaviate: external server (`weaviate` extra).

Index names are derived from a fingerprint of the indexed content, so restarting a server on an
unchanged repository reuses the existing table / collection instead of re-embedding everything.
"""
import hashlib
import json
import logging
import warnings
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import List, Literal, Optional

from .settings import get_settings

logger = logging.getLogger(__name__)

IndexType = Literal['embedding-only', 'keyword-only', 'hybrid']
INDEX_SCHEMA_VERSION = 'v2'


@dataclass
class SearchResult:
    id: str
    name: str = ''
    content: str = ''
    description: str = ''
    path: str = ''
    language: str = ''
    node_type: str = 'chunk'
    order_in_file: int = 0
    declared_entities: List[dict] = field(default_factory=list)
    called_entities: List[str] = field(default_factory=list)
    score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _has_embedding(embedding) -> bool:
    return embedding is not None and len(embedding) > 0


def _loads(value) -> list:
    if isinstance(value, list):
        return value
    try:
        loaded = json.loads(value) if value else []
        return loaded if isinstance(loaded, list) else []
    except (TypeError, ValueError):
        return []


def index_fingerprint(chunk_nodes: list, index_type: str, model_name: str = '') -> str:
    digest = hashlib.sha256()
    digest.update(f'{INDEX_SCHEMA_VERSION}|{index_type}|{model_name}'.encode())
    for node in sorted(chunk_nodes, key=lambda n: n.id):
        digest.update(b'\0' + node.id.encode('utf-8', 'replace'))
        digest.update(b'\0' + node.get_field_to_embed().encode('utf-8', 'replace'))
    return digest.hexdigest()[:16]


class BaseCodeIndex(ABC):
    """Common logic: chunk selection, batch embedding, result conversion."""

    def __init__(self, nodes: list, model_service, index_type: IndexType = 'hybrid',
                 embedding_batch_size: int = 20, use_embed: bool = True, show_progress: bool = False):
        if index_type not in ('embedding-only', 'keyword-only', 'hybrid'):
            raise ValueError(f'Unknown index_type: {index_type}')
        if index_type != 'keyword-only' and model_service is None:
            raise ValueError(f"index_type '{index_type}' requires a model service")
        self.logger = logger
        self.model_service = model_service
        self.index_type = index_type
        self.embedding_batch_size = embedding_batch_size
        self.use_embed = use_embed
        self.show_progress = show_progress
        self.chunk_nodes = [node for node in nodes if getattr(node, 'node_type', None) == 'chunk']
        model_name = getattr(model_service, 'embed_model_name', '') if model_service is not None else ''
        self.fingerprint = index_fingerprint(self.chunk_nodes, index_type, model_name)

    # ------------------------------------------------------------------ embeddings

    def _ensure_embeddings(self) -> None:
        """Embed (in batches) the chunks without a stored embedding, or all of them if use_embed=False."""
        if self.index_type == 'keyword-only':
            return
        todo = [n for n in self.chunk_nodes if not self.use_embed or not _has_embedding(n.embedding)]
        if not todo:
            return
        self.logger.info(f'Embedding {len(todo)} chunks')
        batches = range(0, len(todo), self.embedding_batch_size)
        if self.show_progress:
            from tqdm import tqdm
            batches = tqdm(batches, desc='Embedding chunks')
        for i in batches:
            batch = todo[i:i + self.embedding_batch_size]
            embeddings = self.model_service.embed_chunk_code_batch([n.get_field_to_embed() for n in batch])
            if len(embeddings) != len(batch):
                raise RuntimeError(f'Embedding service returned {len(embeddings)} vectors for {len(batch)} texts')
            for node, embedding in zip(batch, embeddings, strict=True):
                # Stored as a plain list so the graph stays JSON-serializable
                node.embedding = embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)

    @staticmethod
    def _row(node) -> dict:
        return {
            "node_id": node.id,
            "name": node.name,
            "content": node.content,
            "description": node.description or "",
            "path": node.path,
            "language": node.language or "",
            "node_type": node.node_type,
            "order_in_file": int(node.order_in_file),
            "declared_entities": json.dumps(node.declared_entities, ensure_ascii=False, default=str),
            "called_entities": json.dumps(node.called_entities, ensure_ascii=False, default=str),
            # Single column indexed for full-text search (path, name, description and code)
            "search_text": "\n".join(p for p in (node.path, node.name, node.description, node.content) if p),
        }

    @staticmethod
    def _result(props: dict, score: float) -> SearchResult:
        return SearchResult(
            id=props.get('node_id', ''),
            name=props.get('name', ''),
            content=props.get('content', ''),
            description=props.get('description', ''),
            path=props.get('path', ''),
            language=props.get('language', ''),
            node_type=props.get('node_type', 'chunk'),
            order_in_file=int(props.get('order_in_file') or 0),
            declared_entities=_loads(props.get('declared_entities')),
            called_entities=_loads(props.get('called_entities')),
            score=float(score or 0.0),
        )

    # ------------------------------------------------------------------ API

    @abstractmethod
    def search(self, query: str, n_results: int = 10) -> List[SearchResult]:
        """Return the best matching chunks."""

    def query(self, query: str, n_results: int = 10) -> dict:
        """Legacy ChromaDB-like result format: {'ids': [[...]], 'distances', 'metadatas', 'documents'}."""
        results = self.search(query, n_results)
        return {
            'ids': [[r.id for r in results]],
            'distances': [[r.score for r in results]],
            'metadatas': [[{**r.to_dict(), 'order_in_file': str(r.order_in_file),
                            'declared_entities': str(r.declared_entities),
                            'called_entities': str(r.called_entities)} for r in results]],
            'documents': [[r.content for r in results]],
        }

    def close(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release backend resources (connections). The stored index is kept for reuse."""

    def drop(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Delete the stored index."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class LanceDBCodeIndex(BaseCodeIndex):
    """LanceDB-based code index (embedded database)."""

    def __init__(self, nodes: list, model_service, index_type: IndexType = 'hybrid',
                 embedding_batch_size: int = 20, use_embed: bool = True, db_path: Optional[str] = None,
                 table_name: Optional[str] = None, reuse: bool = True, show_progress: bool = False):
        super().__init__(nodes, model_service, index_type, embedding_batch_size, use_embed, show_progress)
        import lancedb

        self.db_path = db_path or get_settings().lancedb_path
        self.db = lancedb.connect(self.db_path)
        self.table_name = table_name or f"code_chunks_{self.fingerprint}"
        self.table = None

        if reuse and self.table_name in self._table_names():
            self.table = self.db.open_table(self.table_name)
            self.logger.info(f'Reusing LanceDB table {self.table_name}')
            return

        self._ensure_embeddings()
        rows = []
        for node in self.chunk_nodes:
            row = self._row(node)
            if self.index_type != "keyword-only":
                row["vector"] = [float(x) for x in node.embedding]
            rows.append(row)
        if not rows:
            self.logger.warning('No chunk to index')
            return

        self.table = self.db.create_table(self.table_name, data=rows, mode='overwrite')
        # Native full-text (BM25) index, used by keyword-only and hybrid search.
        # The user query is passed as a search term, never interpolated into SQL.
        if self.index_type != "embedding-only":
            with warnings.catch_warnings():
                # create_fts_index is flagged deprecated since lancedb 0.25 but remains the only API
                # taking a column name that works across the supported versions (0.25 -> 0.41)
                warnings.simplefilter('ignore')
                self.table.create_fts_index("search_text", use_tantivy=False, replace=True)
        self.logger.info(f'Created LanceDB table {self.table_name} ({len(rows)} chunks)')

    def _table_names(self) -> List[str]:
        try:
            listing = self.db.list_tables()
            names = getattr(listing, 'tables', listing)
        except AttributeError:
            names = self.db.table_names()
        return list(names)

    def search(self, query: str, n_results: int = 10) -> List[SearchResult]:
        if self.table is None or not query or not query.strip():
            return []
        import numpy as np
        try:
            if self.index_type == "keyword-only":
                q = self.table.search(query, query_type="fts", fts_columns="search_text")
                score_column = "_score"
            elif self.index_type == "embedding-only":
                emb = np.array(self.model_service.embed_query(query), dtype=np.float32)
                q = self.table.search(emb, vector_column_name="vector")
                score_column = "_distance"
            else:
                # Vector + BM25 results fused with reciprocal rank fusion (LanceDB default reranker)
                emb = np.array(self.model_service.embed_query(query), dtype=np.float32)
                q = (self.table.search(query_type="hybrid", vector_column_name="vector", fts_columns="search_text")
                     .vector(emb).text(query))
                score_column = "_relevance_score"
            rows = q.limit(n_results).to_list()
        except Exception as e:
            self.logger.error(f'Query failed: {e}', exc_info=True)
            raise
        return [self._result(row, row.get(score_column, 0.0)) for row in rows]

    def drop(self) -> None:
        if self.table_name in self._table_names():
            self.db.drop_table(self.table_name)
        self.table = None


class WeaviateCodeIndex(BaseCodeIndex):
    """Weaviate-based code index (requires a running Weaviate server)."""

    def __init__(self, nodes: list, model_service, index_type: IndexType = 'hybrid',
                 embedding_batch_size: int = 20, use_embed: bool = True, host: Optional[str] = None,
                 port: Optional[int] = None, grpc_port: Optional[int] = None,
                 collection_name: Optional[str] = None, reuse: bool = True, show_progress: bool = False):
        super().__init__(nodes, model_service, index_type, embedding_batch_size, use_embed, show_progress)
        import weaviate
        from weaviate.classes.config import Configure, DataType, Property

        settings = get_settings()
        self.weaviate_client = weaviate.connect_to_local(
            host=host or settings.weaviate_host,
            port=port or settings.weaviate_port,
            grpc_port=grpc_port or settings.weaviate_grpc_port,
        )
        self.alpha = settings.alpha_search_value
        self.collection_name = collection_name or f"CodeChunks_{self.fingerprint}"

        if reuse and self.weaviate_client.collections.exists(self.collection_name):
            self.collection = self.weaviate_client.collections.get(self.collection_name)
            self.logger.info(f'Reusing Weaviate collection {self.collection_name}')
            return

        if self.weaviate_client.collections.exists(self.collection_name):
            self.weaviate_client.collections.delete(self.collection_name)
        text_props = ["node_id", "name", "content", "description", "path", "language", "node_type",
                      "declared_entities", "called_entities", "search_text"]
        self.collection = self.weaviate_client.collections.create(
            name=self.collection_name,
            properties=[Property(name=p, data_type=DataType.TEXT) for p in text_props]
                       + [Property(name="order_in_file", data_type=DataType.INT)],
            vector_config=Configure.Vectors.self_provided(),
        )

        self._ensure_embeddings()
        with self.collection.batch.dynamic() as batch:
            for node in self.chunk_nodes:
                if self.index_type == 'keyword-only':
                    batch.add_object(properties=self._row(node))
                else:
                    batch.add_object(properties=self._row(node), vector=list(node.embedding))

    def search(self, query: str, n_results: int = 10) -> List[SearchResult]:
        if not query or not query.strip():
            return []
        from weaviate.classes.query import MetadataQuery
        if self.index_type == 'keyword-only':
            response = self.collection.query.bm25(query=query, limit=n_results,
                                                  return_metadata=MetadataQuery(score=True))
            return [self._result(o.properties, o.metadata.score) for o in response.objects]
        embedding = self.model_service.embed_query(query)
        if self.index_type == 'embedding-only':
            response = self.collection.query.near_vector(near_vector=embedding, limit=n_results,
                                                         return_metadata=MetadataQuery(distance=True))
            return [self._result(o.properties, o.metadata.distance) for o in response.objects]
        response = self.collection.query.hybrid(query=query, vector=embedding, limit=n_results, alpha=self.alpha,
                                                return_metadata=MetadataQuery(score=True))
        return [self._result(o.properties, o.metadata.score) for o in response.objects]

    def close(self) -> None:
        client = getattr(self, 'weaviate_client', None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def drop(self) -> None:
        self.weaviate_client.collections.delete(self.collection_name)

    def __del__(self):
        self.close()


def CodeIndex(nodes: list, model_service, index_type: IndexType = 'hybrid', embedding_batch_size: int = 20,
              use_embed: bool = True, backend: Optional[Literal['weaviate', 'lancedb']] = None,
              db_path: Optional[str] = None, host: Optional[str] = None, port: Optional[int] = None,
              grpc_port: Optional[int] = None, reuse: bool = True, show_progress: bool = False) -> BaseCodeIndex:
    """
    Create a code index.

    Args:
        nodes: Nodes to index (only chunk nodes are indexed).
        model_service: Embedding service (may be None for 'keyword-only').
        index_type: 'embedding-only', 'keyword-only' or 'hybrid'.
        embedding_batch_size: Batch size for embedding generation.
        use_embed: Reuse embeddings already stored on the nodes (False forces re-embedding).
        backend: 'lancedb' (default, embedded) or 'weaviate' (server). Defaults to INDEX_BACKEND.
        db_path: LanceDB directory.
        host / port / grpc_port: Weaviate connection.
        reuse: Reuse an existing index with the same content fingerprint.
    """
    backend = backend or get_settings().index_backend
    common = dict(nodes=nodes, model_service=model_service, index_type=index_type,
                  embedding_batch_size=embedding_batch_size, use_embed=use_embed, reuse=reuse,
                  show_progress=show_progress)
    if backend == 'lancedb':
        return LanceDBCodeIndex(db_path=db_path, **common)
    if backend == 'weaviate':
        return WeaviateCodeIndex(host=host, port=port, grpc_port=grpc_port, **common)
    raise ValueError(f"Unknown code index backend: '{backend}' (expected 'lancedb' or 'weaviate')")
