"""
Centralised configuration.

All environment variables used by the library are read here, when `get_settings()` is
called (not at import time), so tests and entry points can change the environment
before building a graph. Explicit constructor arguments always win over these values.
"""
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, '') else default


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in (None, '') else default


def _str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value else default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value in (None, ''):
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def _opt_str(name: str) -> Optional[str]:
    return os.getenv(name) or None


@dataclass
class Settings:
    # --- LLM (chat completions, OpenAI-compatible) ---
    openai_base_url: str = 'http://0.0.0.0:8000/v1'
    openai_token: str = 'no-need'
    model_name: str = 'meta-llama/Llama-3.2-3B-Instruct'
    max_tokens: int = 2048
    temperature: float = 0.2
    top_p: float = 0.95
    llm_concurrency: int = 8

    # --- Embeddings ---
    openai_embed_base_url: str = 'http://0.0.0.0:8001/v1'
    openai_embed_token: str = 'no-need'
    embed_model_name: str = 'Alibaba-NLP/gte-Qwen2-1.5B-instruct'

    # --- HTTP / retries ---
    request_timeout: int = 240
    stop_after_attempt: int = 5
    wait_between_retries: int = 2

    # --- Parsing ---
    code_chunk_size: int = 2000
    code_chunk_overlap: int = 0
    max_file_size: int = 1_000_000  # bytes; larger files are skipped

    # --- Code index ---
    index_backend: str = 'lancedb'
    lancedb_path: str = './local_code_index_db'
    weaviate_host: str = 'localhost'
    weaviate_port: int = 8080
    weaviate_grpc_port: int = 50051
    alpha_search_value: float = 0.8

    # --- Git ---
    github_token: Optional[str] = field(default=None, repr=False)

    # --- Java / framework enrichment ---
    java_parser: str = 'tree-sitter'      # 'tree-sitter' (Java 8-21) or 'javalang' (legacy, Java 8)
    enrichers: str = 'auto'               # 'auto', 'none' or a comma-separated list (see enrichers/)
    mask_config_values: bool = True       # mask secret-looking configuration values
    max_call_edges: int = 0               # cap on 'calls' / 'uses_type' edges, 0 = unlimited

    @classmethod
    def from_env(cls) -> 'Settings':
        d = cls()
        return cls(
            openai_base_url=_str('OPENAI_BASE_URL', d.openai_base_url),
            openai_token=_str('OPENAI_TOKEN', d.openai_token),
            model_name=_str('MODEL_NAME', d.model_name),
            max_tokens=_int('MAX_TOKENS', d.max_tokens),
            temperature=_float('TEMPERATURE', d.temperature),
            top_p=_float('TOP_P', d.top_p),
            llm_concurrency=_int('LLM_CONCURRENCY', d.llm_concurrency),
            openai_embed_base_url=_str('OPENAI_EMBED_BASE_URL', d.openai_embed_base_url),
            openai_embed_token=_str('OPENAI_EMBED_TOKEN', d.openai_embed_token),
            embed_model_name=_str('EMBED_MODEL_NAME', d.embed_model_name),
            request_timeout=_int('REQUEST_TIMEOUT', d.request_timeout),
            stop_after_attempt=_int('STOP_AFTER_ATTEMPT', d.stop_after_attempt),
            wait_between_retries=_int('WAIT_BETWEEN_RETRIES', d.wait_between_retries),
            code_chunk_size=_int('CODE_CHUNK_SIZE', d.code_chunk_size),
            code_chunk_overlap=_int('CODE_CHUNK_OVERLAP', d.code_chunk_overlap),
            max_file_size=_int('MAX_FILE_SIZE', d.max_file_size),
            index_backend=_str('INDEX_BACKEND', d.index_backend),
            lancedb_path=_str('LANCEDB_PATH', d.lancedb_path),
            weaviate_host=_str('WEAVIATE_HOST', d.weaviate_host),
            weaviate_port=_int('WEAVIATE_PORT', d.weaviate_port),
            weaviate_grpc_port=_int('WEAVIATE_GRPC_PORT', d.weaviate_grpc_port),
            alpha_search_value=_float('ALPHA_SEARCH_VALUE', d.alpha_search_value),
            github_token=_opt_str('GITHUB_TOKEN') or _opt_str('GITHUB_OAUTH_TOKEN'),
            java_parser=_str('JAVA_PARSER', d.java_parser).lower(),
            enrichers=_str('ENRICHERS', d.enrichers),
            mask_config_values=_bool('MASK_CONFIG_VALUES', d.mask_config_values),
            max_call_edges=_int('MAX_CALL_EDGES', d.max_call_edges),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings read from the environment (cached; call `get_settings.cache_clear()` to reload)."""
    return Settings.from_env()
