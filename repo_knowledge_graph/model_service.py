"""
LLM and embedding services.

* `OpenAIModelService`: chat + embeddings through any OpenAI-compatible API (OpenAI, vLLM...).
* `SentenceTransformersModelService`: chat through the OpenAI-compatible API, embeddings computed
  locally with sentence-transformers (`st` extra; torch is only imported when this service is created).

Configuration comes from `settings.get_settings()`; `model_kwargs` (keys named like the environment
variables, e.g. {"MODEL_NAME": ...}) override it per instance.
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import List, Optional

import httpx
import openai
from openai import AsyncOpenAI, OpenAI
from tenacity import AsyncRetrying, Retrying, retry_if_exception_type, stop_after_attempt, wait_fixed

from .settings import get_settings

logger = logging.getLogger(__name__)

# Only transient failures (network, timeouts, rate limits, 5xx) are worth retrying.
# Programming errors (TypeError, bad request, auth...) must surface immediately.
TRANSIENT_ERRORS = (
    openai.APIConnectionError,  # includes APITimeoutError
    openai.RateLimitError,
    openai.InternalServerError,
    httpx.TransportError,
)


class ModelServiceInterface(ABC):
    """Common chat-completion logic; subclasses implement the embedding methods."""

    def __init__(self, model_name: Optional[str] = None, model_kwargs: Optional[dict] = None):
        self.logger = logger
        settings = get_settings()
        model_kwargs = model_kwargs or {}

        def opt(key: str, default):
            return model_kwargs.get(key, default)

        self.openai_base_url = opt("OPENAI_BASE_URL", settings.openai_base_url)
        self.openai_token = opt("OPENAI_TOKEN", settings.openai_token)
        self.model_name = model_name or opt("MODEL_NAME", settings.model_name)
        self.openai_embed_base_url = opt("OPENAI_EMBED_BASE_URL", settings.openai_embed_base_url)
        self.openai_embed_token = opt("OPENAI_EMBED_TOKEN", settings.openai_embed_token)
        self.embed_model_name = opt("EMBED_MODEL_NAME", settings.embed_model_name)
        self.max_tokens = int(opt("MAX_TOKENS", settings.max_tokens))
        self.temperature = float(opt("TEMPERATURE", settings.temperature))
        self.top_p = float(opt("TOP_P", settings.top_p))
        self.stop_after_attempt = int(opt("STOP_AFTER_ATTEMPT", settings.stop_after_attempt))
        self.wait_between_retries = float(opt("WAIT_BETWEEN_RETRIES", settings.wait_between_retries))
        request_timeout = int(opt("REQUEST_TIMEOUT", settings.request_timeout))

        self.long_timeout_client = httpx.Client(timeout=request_timeout)
        self.long_timeout_async_client = httpx.AsyncClient(timeout=request_timeout)
        self.client = OpenAI(base_url=self.openai_base_url, api_key=self.openai_token,
                             http_client=self.long_timeout_client)
        self.async_client = AsyncOpenAI(base_url=self.openai_base_url, api_key=self.openai_token,
                                        http_client=self.long_timeout_async_client)

    # ------------------------------------------------------------------ retries

    def _retry_kwargs(self) -> dict:
        return dict(retry=retry_if_exception_type(TRANSIENT_ERRORS),
                    stop=stop_after_attempt(self.stop_after_attempt),
                    wait=wait_fixed(self.wait_between_retries), reraise=True)

    def _with_retry(self, fn, *args, **kwargs):
        for attempt in Retrying(**self._retry_kwargs()):
            with attempt:
                return fn(*args, **kwargs)

    async def _with_retry_async(self, fn, *args, **kwargs):
        async for attempt in AsyncRetrying(**self._retry_kwargs()):
            with attempt:
                return await fn(*args, **kwargs)

    # ------------------------------------------------------------------ chat

    def _messages(self, prompt: str, instructions: Optional[str] = None) -> list:
        messages = [{"role": "system", "content": instructions}] if instructions else []
        return messages + [{"role": "user", "content": prompt}]

    def query(self, prompt: str, model_name: Optional[str] = None) -> str:
        """Query the chat model with a prompt."""
        completion = self._with_retry(self.client.chat.completions.create,
                                      model=model_name or self.model_name, messages=self._messages(prompt))
        return completion.choices[0].message.content

    def query_with_instructions(self, prompt: str, instructions: str, model_name: Optional[str] = None) -> str:
        """Query the chat model with additional system instructions."""
        completion = self._with_retry(self.client.chat.completions.create, model=model_name or self.model_name,
                                      messages=self._messages(prompt, instructions))
        return completion.choices[0].message.content

    async def query_async(self, prompt: str, model_name: Optional[str] = None) -> str:
        completion = await self._with_retry_async(self.async_client.chat.completions.create,
                                                  model=model_name or self.model_name,
                                                  messages=self._messages(prompt))
        return completion.choices[0].message.content

    async def query_with_instructions_async(self, prompt: str, instructions: str,
                                            model_name: Optional[str] = None) -> str:
        completion = await self._with_retry_async(self.async_client.chat.completions.create,
                                                  model=model_name or self.model_name,
                                                  messages=self._messages(prompt, instructions))
        return completion.choices[0].message.content

    # ------------------------------------------------------------------ embeddings

    @abstractmethod
    def embed(self, text_to_embed: str) -> list:
        """Embed text using the configured embedding model."""

    async def embed_async(self, text_to_embed: str) -> list:
        """Async version of embed (runs the synchronous call in a worker thread by default)."""
        return await asyncio.to_thread(self.embed, text_to_embed)

    def embed_chunk_code(self, code_to_embed: str) -> list:
        """Embed a code chunk for storage/indexing."""
        return self.embed(code_to_embed)

    @abstractmethod
    def embed_query(self, query_to_embed: str) -> list:
        """Embed a query for retrieval."""

    @abstractmethod
    def embed_batch(self, texts_to_embed: List[str]) -> List[list]:
        """Embed several texts at once."""

    def embed_chunk_code_batch(self, codes_to_embed: List[str]) -> List[list]:
        """Embed several code chunks at once for storage/indexing."""
        return self.embed_batch(codes_to_embed)


class OpenAIModelService(ModelServiceInterface):
    """OpenAI-compatible API for both chat and embeddings."""

    def __init__(self, model_name: Optional[str] = None, embed_model_name: Optional[str] = None,
                 model_kwargs: Optional[dict] = None):
        super().__init__(model_name=model_name, model_kwargs=model_kwargs)
        self.embed_model_name = embed_model_name or self.embed_model_name
        self.embed_client = OpenAI(base_url=self.openai_embed_base_url, api_key=self.openai_embed_token,
                                   http_client=self.long_timeout_client)
        self.async_embed_client = AsyncOpenAI(base_url=self.openai_embed_base_url, api_key=self.openai_embed_token,
                                              http_client=self.long_timeout_async_client)

    def embed(self, text_to_embed: str) -> list:
        response = self._with_retry(self.embed_client.embeddings.create, input=text_to_embed,
                                    model=self.embed_model_name)
        return response.data[0].embedding

    async def embed_async(self, text_to_embed: str) -> list:
        response = await self._with_retry_async(self.async_embed_client.embeddings.create, input=text_to_embed,
                                                model=self.embed_model_name)
        return response.data[0].embedding

    def embed_query(self, query_to_embed: str) -> list:
        return self.embed(query_to_embed)

    def embed_batch(self, texts_to_embed: List[str]) -> List[list]:
        if not texts_to_embed:
            return []
        response = self._with_retry(self.embed_client.embeddings.create, input=texts_to_embed,
                                    model=self.embed_model_name)
        return [item.embedding for item in response.data]


class SentenceTransformersModelService(ModelServiceInterface):
    """OpenAI-compatible API for chat, local sentence-transformers model for embeddings."""

    QUERY_PROMPT = 'Given this prompt, retrieve relevant content\n Query:'

    def __init__(self, model_name: Optional[str] = None, embed_model_name: Optional[str] = None,
                 model_kwargs: Optional[dict] = None, device: Optional[str] = None):
        super().__init__(model_name=model_name, model_kwargs=model_kwargs)
        self.embed_model_name = embed_model_name or self.embed_model_name
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ImportError("sentence-transformers is required for embedder_type='sentence-transformers' "
                              "(pip install 'repo-knowledge-graph[st]')") from e
        if device is None:
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"
        self.device = device
        self.logger.info(f'Loading SentenceTransformer {self.embed_model_name} on {self.device}')
        self.embedding_model = SentenceTransformer(self.embed_model_name, trust_remote_code=True, device=self.device)

    @staticmethod
    def _to_list(embedding) -> list:
        return embedding.tolist() if hasattr(embedding, 'tolist') else list(embedding)

    def embed(self, text_to_embed: str) -> list:
        return self._to_list(self.embedding_model.encode([text_to_embed])[0])

    def embed_query(self, query_to_embed: str) -> list:
        return self._to_list(self.embedding_model.encode([query_to_embed], prompt=self.QUERY_PROMPT)[0])

    def embed_batch(self, texts_to_embed: List[str]) -> List[list]:
        if not texts_to_embed:
            return []
        return [self._to_list(e) for e in self.embedding_model.encode(texts_to_embed)]


def create_model_service(**kwargs) -> ModelServiceInterface:
    """
    Create the model service selected by `embedder_type` ('openai' or 'sentence-transformers').

    Other keyword arguments are passed to the service (model_name, embed_model_name, model_kwargs).
    """
    model_kwargs = kwargs.pop('model_kwargs', None)
    embedder_type = kwargs.pop('embedder_type', 'openai')
    if embedder_type == 'sentence-transformers':
        return SentenceTransformersModelService(model_kwargs=model_kwargs, **kwargs)
    if embedder_type != 'openai':
        logger.warning(f'Unknown embedder type: {embedder_type}, defaulting to OpenAI')
    return OpenAIModelService(model_kwargs=model_kwargs, **kwargs)
