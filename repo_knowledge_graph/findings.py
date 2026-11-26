"""
Candidate findings for an auditor: one list gathering every security fact of the graph, each
with a stable id, CWE, severity, confidence, location, evidence and a hint to verify it, plus a SARIF 2.1.0
export (GitHub code scanning, IDEs).

Sources: sinks reached by untrusted data (`tainted`, with their taint flows), weak values and dangerous APIs,
secrets written in the code, the security posture, and the known vulnerabilities of the dependencies
when `check_dependencies` was run. Unconfirmed sinks (`depends_on_*`, `unknown`) are left out unless asked:
they are leads, not findings. Everything here is a candidate to verify, never a verdict.
"""
import hashlib
from typing import Any, Dict, List, Optional

from .node import ChunkNode, EntityNode, FileNode, ResourceNode

SEVERITY_RANK = {'high': 0, 'medium': 1, 'low': 2}
CONFIDENCE_RANK = {'high': 0, 'medium': 1, 'low': 2}
SARIF_LEVEL = {'high': 'error', 'medium': 'warning', 'low': 'note'}
UNCONFIRMED = ('depends_on_param', 'depends_on_field', 'depends_on_call', 'unknown')

HINTS = {
    'sql_injection': "Send a quote or `' OR '1'='1` in {input} and compare the answers; write a MockMvc test that "
                     "asserts the query is not altered.",
    'command_injection': 'Send `; id` (or `& whoami`) in {input} and look for its output or side effect.',
    'path_traversal': 'Send `../../etc/passwd` (or an absolute path) in {input} and check which file is read.',
    'ssrf': 'Point {input} to a host you control (or 169.254.169.254) and watch for the request.',
    'expression_injection': 'Send `T(java.lang.Runtime).getRuntime()` or `7*7` in {input} and check the evaluation.',
    'ldap_injection': 'Send `*)(uid=*))(|(uid=*` in {input} and compare the results.',
    'xpath_injection': "Send `' or '1'='1` in {input} and compare the results.",
    'open_redirect': 'Send an absolute URL (https://example.org) in {input} and check the Location header.',
    'log_injection': 'Send a value with a line break (%0a) in {input} and look for a forged log line.',
    'header_injection': 'Send a value with CRLF (%0d%0a) in {input} and look for an injected header.',
    'xss': 'Send `<script>alert(1)</script>` in {input} and check the rendered response.',
    'xxe': 'Send an XML document declaring an external entity (file:///etc/hostname) and check the answer.',
    'deserialization': 'Check where the serialized data comes from; a gadget chain needs attacker-controlled input.',
    'weak_hash': 'Check what is hashed: passwords and signatures need a strong algorithm (bcrypt, SHA-256+).',
    'weak_crypto': 'Check the algorithm and mode; prefer AES/GCM with a random IV.',
    'hardcoded_key': 'Check whether the key is used in production; move it to a secret store.',
    'insecure_randomness': 'Check whether the value is a secret (token, password, id); use SecureRandom.',
    'trust_all_certificates': 'Check whether this code runs in production; it accepts any certificate.',
}
POSTURE_HINTS = {
    'access.public_state_changing': 'Call the endpoint without credentials and check whether the change is applied.',
    'access.annotation_not_enforced': 'Call the endpoint with a user lacking the role: it should be refused.',
    'access.idor_candidate': "Call the endpoint with another user's identifier and check whether the object is returned.",
    'input.mass_assignment': 'Send extra fields (id, role, owner) in the body and check whether they are persisted.',
    'input.missing_validation': 'Send empty, oversized or malformed fields and check the behaviour.',
    'output.sensitive_fields': 'Call the endpoint and check whether the sensitive field appears in the JSON.',
    'output.error_details': 'Trigger the error and check whether internal details reach the response.',
    'config.csrf_disabled': 'Replay a state-changing request from another origin with the session cookie.',
    'config.cors_wildcard_credentials': 'Send a request with Origin: https://evil.example and credentials; check the CORS headers.',
    'config.shadowed_rule': 'Call a path of the shadowed rule with a user lacking its role.',
}


def _fid(*parts: Any) -> str:
    return 'F-' + hashlib.sha1('|'.join(str(p) for p in parts).encode('utf-8')).hexdigest()[:10]


def _entry_of(flows: List[Dict[str, Any]]) -> Optional[str]:
    entries = [f['source'].get('entry') for f in flows if f['source'].get('entry')]
    return entries[0] if entries else None


def collect_findings(kg, include_unconfirmed: bool = False) -> List[Dict[str, Any]]:
    from .enrichers.security import STATUS_RANK  # noqa: F401  (statuses documented there)
    found: List[Dict[str, Any]] = []
    for node_id, data in kg.graph.nodes(data=True):
        node = data.get('data')
        if isinstance(node, EntityNode):
            security = (node.attributes or {}).get('security')
            for sink in (security or {}).get('sinks') or [] if isinstance(security, dict) else []:
                status = sink.get('status')
                if status in ('safe_by_construction', 'sanitized', 'mitigated'):
                    continue
                if status in UNCONFIRMED and (not include_unconfirmed or sink.get('reachable_from_source') is False):
                    continue
                flows = sink.get('flows') or []
                entry = _entry_of(flows)
                source = flows[0]['source'] if flows else {}
                origin = source.get('param') or source.get('call') or ', '.join(sink.get('origins') or [])
                confidence = sink.get('confidence') or ('high' if status in ('tainted', 'weak_value', 'dangerous_api')
                                                        else 'low')
                severity = sink.get('severity') or 'medium'
                if status in UNCONFIRMED:
                    severity = 'low'
                where = f"parameter {origin}" + (f' of {entry}' if entry else '') if origin else 'the input'
                found.append({
                    'id': _fid(sink.get('id'), sink.get('file'), sink.get('line'), node_id),
                    'rule': f"sink.{sink.get('category')}", 'kind': 'taint' if status == 'tainted' else 'sink',
                    'cwe': sink.get('cwe'), 'severity': severity, 'confidence': confidence,
                    'title': f"{sink.get('category', '').replace('_', ' ').capitalize()}: {sink.get('api')} "
                             + ({'tainted': 'reached by untrusted data', 'weak_value': 'with a weak value',
                                 'dangerous_api': 'is dangerous whatever its arguments'}.get(status, f'({status})')),
                    'file': sink.get('file'), 'line': sink.get('line'), 'method': node_id, 'entry': entry,
                    'status': status, 'local_status': sink.get('local_status'),
                    'evidence': _flow_text(flows[0], sink) if flows else (', '.join(sink.get('origins') or [])
                                                                           or sink.get('value')),
                    'flows': flows, 'hint': HINTS.get(str(sink.get('category')), 'Read the code at the location.')
                    .format(input=where)})
        elif isinstance(node, FileNode) and not isinstance(node, ChunkNode):
            for secret in (node.attributes or {}).get('hardcoded_secrets') or []:
                found.append({'id': _fid('secret', node_id, secret.get('line')), 'rule': 'code.hardcoded_secret',
                              'kind': 'secret', 'cwe': 798, 'severity': 'medium', 'confidence': 'medium',
                              'title': f"Secret written in the code ({secret.get('kind')})", 'file': node_id,
                              'line': secret.get('line'),
                              'hint': 'Check whether the value is real; move it to an environment variable or a vault.'})
        elif isinstance(node, ResourceNode) and node.node_type == 'security_posture':
            for item in node.attributes.get('findings') or []:
                found.append({'id': _fid(item['id'], item.get('endpoint'), item.get('file'), item.get('line'),
                                         item.get('property'), item.get('method')),
                              'rule': item['id'], 'kind': 'posture', 'cwe': item.get('cwe'),
                              'severity': item.get('severity', 'medium'), 'confidence': item.get('confidence', 'medium'),
                              'title': item.get('title'), 'file': item.get('file'), 'line': item.get('line'),
                              'entry': item.get('endpoint'), 'method': item.get('method'),
                              'evidence': item.get('property') and f"{item['property']}={item.get('value', '***')}",
                              'hint': item.get('hint') or POSTURE_HINTS.get(item['id'], 'Check the configuration.')})
        elif isinstance(node, ResourceNode) and node.node_type == 'dependency':
            for vuln in node.attributes.get('vulnerabilities') or []:
                reach = node.attributes.get('reachability') or {}
                found.append({'id': _fid('dep', node_id, vuln.get('id')), 'rule': 'dependency.known_vulnerability',
                              'kind': 'dependency', 'cwe': 1395, 'severity': vuln.get('severity', 'medium'),
                              'confidence': 'high' if reach.get('entry_points') else 'medium' if reach.get('files') else 'low',
                              'title': f"{node.key} {node.attributes.get('resolved_version')}: {vuln.get('id')} "
                                       f"{' '.join(vuln.get('aliases') or [])} {vuln.get('summary', '')}".strip()[:200],
                              'file': node.attributes.get('declared_in'), 'line': None,
                              'evidence': f"reached from {', '.join(e['entry'] for e in reach.get('entry_points', [])[:3])}"
                              if reach.get('entry_points') else f"imported by {len(reach.get('files') or [])} file(s)",
                              'hint': f"Upgrade to {', '.join(vuln.get('fixed_in') or ['a fixed version'])}; check whether "
                                      'the vulnerable function is used.', 'url': vuln.get('url')})
    for item in found:  # test code is not deployed: keep it, ranked lower
        if '/test/' in f"/{item.get('file') or ''}" and item['kind'] in ('taint', 'sink', 'secret'):
            item['severity'], item['test_code'] = 'low', True
    return sorted(found, key=lambda f: (SEVERITY_RANK.get(str(f['severity']), 3),
                                        CONFIDENCE_RANK.get(str(f['confidence']), 3), str(f.get('file')), f.get('line') or 0))


def _flow_text(flow: Dict[str, Any], sink: Dict[str, Any]) -> str:
    source = flow['source']
    parts = [f"{source.get('entry') or source.get('method')} ({source.get('param') or source.get('call')})"]
    parts += [f"{s['to'].rsplit('.', 1)[-1]}({s.get('param')}) at {s.get('file')}:{s.get('line')}" for s in flow['steps']]
    parts.append(f"{sink.get('api')} at {sink.get('file')}:{sink.get('line')}")
    return ' -> '.join(parts)


def filter_findings(findings: List[Dict[str, Any]], severity: Optional[str] = None, cwe: Optional[int] = None,
                    kind: Optional[str] = None, min_confidence: Optional[str] = None,
                    path_prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    worst = SEVERITY_RANK.get(severity or 'low', 2)
    least = CONFIDENCE_RANK.get(min_confidence or 'low', 2)
    return [f for f in findings
            if SEVERITY_RANK.get(str(f['severity']), 2) <= worst
            and CONFIDENCE_RANK.get(str(f['confidence']), 2) <= least
            and (cwe is None or f.get('cwe') == cwe)
            and (kind is None or f.get('kind') == kind)
            and (not path_prefix or str(f.get('file') or '').startswith(path_prefix))]


def to_sarif(findings: List[Dict[str, Any]], tool_version: str = '') -> Dict[str, Any]:
    """SARIF 2.1.0 log: one rule per finding rule, taint flows as code flows."""
    rules: Dict[str, Dict[str, Any]] = {}
    results = []
    for f in findings:
        rule_id = f['rule']
        if rule_id not in rules:
            rules[rule_id] = {
                'id': rule_id, 'name': rule_id.replace('.', '_'),
                'shortDescription': {'text': str(f['title']).split(':')[0][:120]},
                'defaultConfiguration': {'level': SARIF_LEVEL.get(str(f['severity']), 'warning')},
                'properties': {'tags': ['security'] + ([f"external/cwe/cwe-{f['cwe']}"] if f.get('cwe') else [])}}
        result: Dict[str, Any] = {
            'ruleId': rule_id, 'level': SARIF_LEVEL.get(str(f['severity']), 'warning'),
            'message': {'text': f"{f['title']}. {f.get('hint', '')}".strip()},
            'partialFingerprints': {'findingId': f['id']},
            'properties': {k: f[k] for k in ('confidence', 'cwe', 'entry', 'kind', 'status') if f.get(k)}}
        if f.get('file'):
            result['locations'] = [_location(f['file'], f.get('line'))]
        flows = [fl for fl in f.get('flows') or [] if fl.get('steps') or fl['source'].get('file')]
        if flows:
            result['codeFlows'] = [{'threadFlows': [{'locations': [
                {'location': {**_location(fl['source'].get('file') or f['file'], None),
                              'message': {'text': f"source: {fl['source'].get('entry') or ''} "
                                                  f"{fl['source'].get('param') or fl['source'].get('call') or ''}".strip()}}}]
                + [{'location': {**_location(s.get('file'), s.get('line')),
                                 'message': {'text': f"calls {s['to'].rsplit('.', 1)[-1]}({s.get('param')})"}}}
                   for s in fl['steps']]
                + [{'location': {**_location(f['file'], f.get('line')), 'message': {'text': 'sink'}}}]}]}
                for fl in flows[:3]]
        results.append(result)
    return {
        '$schema': 'https://json.schemastore.org/sarif-2.1.0.json', 'version': '2.1.0',
        'runs': [{'tool': {'driver': {'name': 'repo-knowledge-graph', 'version': tool_version or '0',
                                      'rules': list(rules.values())}},
                  'results': results}]}


def _location(path: Optional[str], line: Optional[int]) -> Dict[str, Any]:
    physical: Dict[str, Any] = {'artifactLocation': {'uri': str(path or '').replace('\\', '/')}}
    if line:
        physical['region'] = {'startLine': int(line)}
    return {'physicalLocation': physical}


__all__ = ['collect_findings', 'filter_findings', 'to_sarif', 'HINTS', 'POSTURE_HINTS']
