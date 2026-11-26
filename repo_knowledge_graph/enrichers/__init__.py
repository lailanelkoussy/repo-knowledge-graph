"""
Framework enrichers: add framework knowledge (build modules, Spring beans and injection, web endpoints,
OpenAPI contracts, GraphQL resolvers, JPA / MyBatis persistence, messaging, configuration, tests) on top of
the generic code graph.

Selection (GraphBuilder `enrichers` argument, `--enrichers` option, ENRICHERS variable):
    'auto'  every enricher whose `applies()` detects its framework in the repository (default)
    'none'  no enrichment (the graph only holds the generic relations)
    'a,b'   only these enrichers, in the canonical order below
"""
import logging
from typing import Any, Callable, Dict, List, Optional

import networkx as nx

from ..entity_registry import EntityRegistry
from .base import EnrichmentContext

logger = logging.getLogger(__name__)


def _registry() -> Dict[str, Any]:
    from . import (
        build_files,
        graphql,
        mybatis,
        openapi,
        security,
        spring_beans,
        spring_config,
        spring_data_jpa,
        spring_messaging,
        spring_security,
        spring_tests,
        spring_web,
        supply_chain,
        web_routes,
    )
    # Order matters: configuration values are used to resolve '${...}' placeholders of endpoints,
    # beans are needed to resolve injections in the web / messaging layers, OpenAPI operations are
    # turned into endpoints by spring_web.
    return {
        'build': build_files,
        'supply_chain': supply_chain,  # resolved versions, file -> library links
        'spring_config': spring_config,
        'spring_beans': spring_beans,
        'openapi': openapi,
        'spring_web': spring_web,
        'web_routes': web_routes,
        'graphql': graphql,
        'spring_data_jpa': spring_data_jpa,
        'mybatis': mybatis,
        'spring_messaging': spring_messaging,
        'spring_tests': spring_tests,
        'spring_security': spring_security,  # access matrix: needs endpoints, configuration and JPA entities
        'security': security,  # last: classifies sinks with everything the other enrichers found
    }


def available_enrichers() -> List[str]:
    return list(_registry())


def select_enrichers(selection: Optional[str]) -> List[str]:
    names = available_enrichers()
    value = (selection or 'auto').strip().lower()
    if value in ('auto', 'all', ''):
        return names
    if value in ('none', 'off', 'false', '0'):
        return []
    wanted = {v.strip() for v in value.split(',') if v.strip()}
    unknown = wanted - set(names)
    if unknown:
        raise ValueError(f"Unknown enricher(s): {', '.join(sorted(unknown))}. Available: {', '.join(names)}")
    return [n for n in names if n in wanted]


def run_enrichers(graph: nx.DiGraph, registry: EntityRegistry, analyses: Dict[str, Any],
                  selection: Optional[str] = 'auto', mask_config_values: bool = True) -> List[str]:
    """Run the selected enrichers; returns the names of those that were applied."""
    names = select_enrichers(selection)
    if not names or not analyses:
        return []
    explicit = (selection or 'auto').strip().lower() not in ('auto', 'all', '')
    context = EnrichmentContext(graph, registry, analyses, mask_config_values=mask_config_values)
    modules = _registry()
    applied = []
    for name in names:
        module = modules[name]
        applies: Callable[[EnrichmentContext], bool] = module.applies
        try:
            if not explicit and not applies(context):
                continue
            module.enrich(context)
            applied.append(name)
        except Exception as e:  # an enricher must never break the build
            logger.error(f'Enricher {name} failed: {e}', exc_info=True)
    if applied:
        logger.info(f"Enrichers applied: {', '.join(applied)}")
    graph.graph['enrichers'] = applied
    return applied


__all__ = ['available_enrichers', 'run_enrichers', 'select_enrichers', 'EnrichmentContext']
