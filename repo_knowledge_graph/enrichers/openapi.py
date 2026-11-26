"""
OpenAPI / Swagger specifications (contract-first APIs).

With openapi-generator (`interfaceOnly`), the REST mappings live in API interfaces generated at build time
(`OwnersApi`, `PettypesApi`...): they are not in the repository, and the controllers implementing them carry
no @GetMapping. This enricher reads the specification files (`openapi.yml`, `swagger.json`...) and
matches every operation to the controller method implementing it:

    operation (path, method, operationId, tags)
      -> controller implementing `<Tag>Api` (openapi-generator naming: camelize(tag) + 'Api')
      -> its method named after the operationId

Matched operations are handed to `spring_web` (context.shared['openapi_operations']) which creates the
`endpoint` nodes like for annotated controllers (class-level @RequestMapping prefix, security, filter chain,
MockMvc callers...), enriched with the specification: summary, parameters, request / response schemas.
Operations without implementation become endpoint nodes with `implemented: false`.
"""
import json
import logging
import posixpath
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .base import EnrichmentContext

logger = logging.getLogger(__name__)

HTTP_METHODS = ('get', 'post', 'put', 'patch', 'delete', 'head', 'options')
SPEC_EXTENSIONS = ('.yml', '.yaml', '.json')
SPEC_MARKER = re.compile(r'^\s*["\']?(openapi|swagger)["\']?\s*:', re.M)


def camelize(text: str) -> str:
    """openapi-generator class name of a tag: 'owner-v2' -> 'OwnerV2', 'pettypes' -> 'Pettypes'."""
    parts = [p for p in re.split(r'[^A-Za-z0-9]+', text or '') if p]
    return ''.join(p[:1].upper() + p[1:] for p in parts) or 'Default'


def _load(path: str, content: str) -> Optional[Dict[str, Any]]:
    if not SPEC_MARKER.search(content[:4000]):
        return None
    try:
        if path.lower().endswith('.json'):
            data = json.loads(content)
        else:
            import yaml
            data = yaml.safe_load(content)
    except Exception as e:  # invalid spec: never break the build
        logger.warning(f'Cannot parse OpenAPI specification {path}: {e}')
        return None
    if isinstance(data, dict) and ('openapi' in data or 'swagger' in data) and isinstance(data.get('paths'), dict):
        return data
    return None


def spec_files(context: EnrichmentContext) -> Iterator[Tuple[str, Dict[str, Any]]]:
    for path, content in context.files(*SPEC_EXTENSIONS):
        if posixpath.basename(path).lower() in ('package.json', 'package-lock.json', 'tsconfig.json'):
            continue
        data = _load(path, content)
        if data is not None:
            yield path, data


def applies(context: EnrichmentContext) -> bool:
    return next(spec_files(context), None) is not None


def _ref_name(value: Any) -> Optional[str]:
    """'#/components/schemas/Owner' -> 'Owner'; inline schemas -> their type ('array of Owner'...)."""
    if not isinstance(value, dict):
        return None
    if '$ref' in value:
        return str(value['$ref']).rsplit('/', 1)[-1]
    if value.get('type') == 'array':
        item = _ref_name(value.get('items'))
        return f'array of {item}' if item else 'array'
    return value.get('type')


def _resolve(spec: Dict[str, Any], value: Any) -> Any:
    """Follow one local $ref ('#/components/parameters/x')."""
    if isinstance(value, dict) and isinstance(value.get('$ref'), str) and value['$ref'].startswith('#/'):
        target: Any = spec
        for part in value['$ref'][2:].split('/'):
            target = target.get(part) if isinstance(target, dict) else None
        return target if isinstance(target, dict) else value
    return value


def _media_schema(content: Any) -> Optional[str]:
    if not isinstance(content, dict):
        return None
    for media in content.values():
        if isinstance(media, dict):
            name = _ref_name(media.get('schema'))
            if name:
                return name
    return None


def parse_operations(path: str, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    operations = []
    base_path = spec.get('basePath') or ''  # Swagger 2
    servers = [s.get('url') for s in spec.get('servers') or [] if isinstance(s, dict) and s.get('url')]
    for route, item in (spec.get('paths') or {}).items():
        if not isinstance(item, dict):
            continue
        shared_parameters = item.get('parameters') or []
        for method in HTTP_METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            parameters: Dict[str, List[Dict[str, Any]]] = {}
            request_body = None
            for raw in list(shared_parameters) + list(operation.get('parameters') or []):
                parameter = _resolve(spec, raw)
                if not isinstance(parameter, dict):
                    continue
                location = parameter.get('in', 'query')
                if location == 'body':  # Swagger 2
                    request_body = {'schema': _ref_name(parameter.get('schema')), 'required': parameter.get('required')}
                    continue
                record = {'name': parameter.get('name'), 'required': bool(parameter.get('required')),
                          'type': _ref_name(parameter.get('schema')) or parameter.get('type')}
                parameters.setdefault(location, []).append({k: v for k, v in record.items() if v is not None})
            body = _resolve(spec, operation.get('requestBody'))
            if isinstance(body, dict):
                request_body = {'schema': _media_schema(body.get('content')), 'required': body.get('required')}
            responses = {}
            for status, response in (operation.get('responses') or {}).items():
                response = _resolve(spec, response)
                if isinstance(response, dict):
                    schema = _media_schema(response.get('content')) or _ref_name(response.get('schema'))
                    responses[str(status)] = schema or response.get('description', '')[:80]
            operations.append({
                'spec_file': path,
                'path': (base_path.rstrip('/') + route) if base_path else route,
                'method': method.upper(),
                'operation_id': operation.get('operationId'),
                'tags': [t for t in operation.get('tags') or [] if isinstance(t, str)],
                'summary': operation.get('summary') or operation.get('description'),
                'parameters': parameters,
                'request_body': request_body,
                'responses': responses,
                'deprecated': bool(operation.get('deprecated')),
                'security': operation.get('security', spec.get('security')),
                'servers': servers,
            })
    return operations


def _implementations(context: EnrichmentContext) -> Dict[str, List[str]]:
    """API interface simple name -> classes implementing it (interfaces generated outside the repository
    or declared in it)."""
    result: Dict[str, List[str]] = {}
    for type_name, decl in context.java_types.items():
        if decl.get('type') not in ('class', 'record'):
            continue
        for base in decl.get('bases') or []:
            simple = base.rsplit('.', 1)[-1]
            if simple.endswith('Api') or simple.endswith('API'):
                result.setdefault(simple, []).append(type_name)
    return result


def _methods(context: EnrichmentContext, type_name: str) -> Dict[str, str]:
    methods: Dict[str, str] = {}
    for member in context.members_of(type_name):
        if member.get('type') in ('method', 'api_endpoint'):
            canonical = context.registry.lookup(member['name'])
            if canonical:
                methods.setdefault(member['name'].rsplit('::', 1)[-1], canonical)
    return methods


def enrich(context: EnrichmentContext) -> None:
    implementations = _implementations(context)
    all_implementers = sorted({c for classes in implementations.values() for c in classes})
    methods_cache: Dict[str, Dict[str, str]] = {}

    def methods_of(type_name: str) -> Dict[str, str]:
        if type_name not in methods_cache:
            methods_cache[type_name] = _methods(context, type_name)
        return methods_cache[type_name]

    matched, unmatched = [], []
    for path, spec in spec_files(context):
        for operation in parse_operations(path, spec):
            operation_id = operation['operation_id']
            handler, via = None, None
            if operation_id:
                interfaces = [camelize(t) + 'Api' for t in operation['tags']]
                if not interfaces:
                    first_segment = next((s for s in operation['path'].split('/') if s and '{' not in s), 'default')
                    interfaces = [camelize(first_segment) + 'Api', 'DefaultApi']
                candidates = [c for i in interfaces for c in implementations.get(i, []) if operation_id in methods_of(c)]
                via = 'tag'
                if not candidates:  # tags renamed or no tags: any class implementing an *Api interface
                    candidates = [c for c in all_implementers if operation_id in methods_of(c)]
                    via = 'operation_id'
                candidates = list(dict.fromkeys(candidates))
                if len(candidates) == 1:
                    handler = methods_of(candidates[0])[operation_id]
                elif len(candidates) > 1:
                    operation['ambiguous_handlers'] = [methods_of(c)[operation_id] for c in candidates]
            if handler:
                operation['handler'] = handler
                operation['match'] = via
                matched.append(operation)
            else:
                unmatched.append(operation)
    context.shared['openapi_operations'] = matched
    context.shared['openapi_unimplemented'] = unmatched
    logger.info(f'OpenAPI: {len(matched)} operation(s) matched to controller methods, {len(unmatched)} unmatched')
