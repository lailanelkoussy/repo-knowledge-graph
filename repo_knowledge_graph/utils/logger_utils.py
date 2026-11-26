"""
Logging helpers.

The library itself only creates loggers (`logging.getLogger(__name__)`) and never
installs handlers, creates log directories or writes to stdout. Applications
(CLI, MCP server, scripts) call `configure_logging()` once at start-up.
Logs go to stderr so they never corrupt the MCP `stdio` transport.
"""
import logging
import sys
from typing import Optional, Union

LIBRARY_LOGGER = 'repo_knowledge_graph'

logging.getLogger(LIBRARY_LOGGER).addHandler(logging.NullHandler())


def configure_logging(level: Union[int, str] = logging.INFO, log_file: Optional[str] = None) -> None:
    """Configure the library logger for an application entry point (idempotent)."""
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
    logger = logging.getLogger(LIBRARY_LOGGER)
    logger.setLevel(level)
    for handler in list(logger.handlers):
        if not isinstance(handler, logging.NullHandler):
            logger.removeHandler(handler)
            handler.close()
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    handlers = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding='utf-8'))
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
