"""
Web routes of non-Java frameworks: the `api_endpoint` entities already detected by the Python (Flask,
FastAPI, Django REST), Rust (Actix, Rocket, Axum) and JavaScript extractors become `endpoint` nodes with a
`handled_by` edge, so that `list_endpoints` / `trace_flow` work the same way as for Spring.
"""
from .base import EnrichmentContext
from .spring_web import normalize_path


def _route_declarations(context: EnrichmentContext):
    for path in sorted(context.analyses):
        if path.lower().endswith('.java'):
            continue  # Spring endpoints are handled by spring_web
        for entity in context.file_declarations(path):
            if entity.get('type') == 'api_endpoint' and entity.get('endpoint'):
                yield path, entity


def applies(context: EnrichmentContext) -> bool:
    return next(_route_declarations(context), None) is not None


def enrich(context: EnrichmentContext) -> None:
    for path, entity in _route_declarations(context):
        handler = context.registry.lookup(entity['name'])
        if not handler:
            continue
        route = normalize_path(str(entity['endpoint']))
        for method in entity.get('methods') or ['ANY']:
            method = str(method).upper()
            endpoint_id = context.add_resource('endpoint', f'{method} {route}', {
                'http_method': method, 'path': route, 'handler': handler, 'external': False,
                'framework': (entity.get('attributes') or {}).get('framework')}, file_path=path)
            context.add_edge(endpoint_id, handler, 'handled_by')
