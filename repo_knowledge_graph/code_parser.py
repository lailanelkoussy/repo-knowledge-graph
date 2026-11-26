import logging
from typing import List, Optional

from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

from .languages import get_language_spec
from .settings import get_settings

logger = logging.getLogger(__name__)


class CodeParser:
    """Split a file into chunks, using a language-aware splitter when the language is known."""

    def __init__(self, chunk_size: Optional[int] = None, chunk_overlap: Optional[int] = None):
        settings = get_settings()
        self.chunk_size = chunk_size or settings.code_chunk_size
        self.chunk_overlap = settings.code_chunk_overlap if chunk_overlap is None else chunk_overlap
        self.logger = logger

    def _splitter_for(self, file_name: str) -> RecursiveCharacterTextSplitter:
        spec = get_language_spec(file_name)
        if spec and spec.splitter and hasattr(Language, spec.splitter):
            return RecursiveCharacterTextSplitter.from_language(
                language=getattr(Language, spec.splitter),
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
            )
        return RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            is_separator_regex=False,
        )

    def parse(self, file_name: str, file_content: str) -> List[str]:
        if not file_content:
            return []
        try:
            docs = self._splitter_for(file_name).create_documents([file_content])
        except Exception as e:
            # Fall back to a single chunk holding the whole file rather than failing the whole build
            self.logger.error(f'Error when parsing {file_name}, keeping it as a single chunk: {e}')
            return [file_content]
        return [doc.page_content for doc in docs]
