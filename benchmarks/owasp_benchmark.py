"""
Score the security analysis on the OWASP Benchmark for Java (https://github.com/OWASP-Benchmark/BenchmarkJava).

Each BenchmarkTestNNNNN servlet is labelled (expectedresults-*.csv): category, real vulnerability or not, CWE.
A test counts as flagged when the graph reports, in that file, a finding of the matching category:
a `tainted` sink for the injection categories, a `weak_value` / `dangerous_api` sink for the crypto ones.
The score of a category is the true positive rate minus the false positive rate (Youden index), as in the
Benchmark's own scorecards.

Usage (no network unless --clone is given):
    python benchmarks/owasp_benchmark.py --benchmark-dir ../BenchmarkJava [--clone] [--json results.json]
"""
import argparse
import csv
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from repo_knowledge_graph import RepoKnowledgeGraph  # noqa: E402
from repo_knowledge_graph.node import EntityNode  # noqa: E402

REPO_URL = 'https://github.com/OWASP-Benchmark/BenchmarkJava.git'
# Benchmark category -> (our sink categories, statuses that count as a detection)
TAINT = ('tainted',)
WEAK = ('weak_value', 'dangerous_api')
CATEGORIES: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    'sqli': (('sql_injection',), TAINT),
    'cmdi': (('command_injection',), TAINT),
    'pathtraver': (('path_traversal',), TAINT),
    'ldapi': (('ldap_injection',), TAINT),
    'xpathi': (('xpath_injection',), TAINT),
    'xss': (('xss',), TAINT),
    'trustbound': (('trust_boundary',), TAINT),
    'crypto': (('weak_crypto',), WEAK),
    'hash': (('weak_hash',), WEAK),
    'weakrand': (('insecure_randomness',), WEAK),
    'securecookie': (('insecure_cookie',), WEAK),
}


def expected(benchmark: Path) -> Dict[str, Tuple[str, bool, int]]:
    csv_file = next(benchmark.glob('expectedresults-*.csv'))
    labels = {}
    with csv_file.open(encoding='utf-8') as handle:
        for row in csv.reader(handle):
            if not row or row[0].startswith('#'):
                continue
            labels[row[0].strip()] = (row[1].strip(), row[2].strip().lower() == 'true', int(row[3]))
    return labels


def detections(kg) -> Dict[str, Set[Tuple[str, str]]]:
    """test name -> {(sink category, status)} found in BenchmarkTestNNNNN.java (helpers excluded)."""
    found: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    for _, data in kg.graph.nodes(data=True):
        node = data.get('data')
        if not isinstance(node, EntityNode):
            continue
        security = (node.attributes or {}).get('security')
        if not isinstance(security, dict):
            continue
        for sink in security.get('sinks') or []:
            name = Path(str(sink.get('file') or '')).stem
            if name.startswith('BenchmarkTest'):
                found[name].add((sink.get('category'), sink.get('status')))
    return found


def score(labels, found) -> Dict[str, Dict[str, float]]:
    table: Dict[str, Dict[str, float]] = {}
    for category, (ours, statuses) in CATEGORIES.items():
        tp = fp = fn = tn = 0
        for test, (cat, real, _) in labels.items():
            if cat != category:
                continue
            flagged = any(c in ours and s in statuses for c, s in found.get(test, ()))
            if real and flagged:
                tp += 1
            elif real:
                fn += 1
            elif flagged:
                fp += 1
            else:
                tn += 1
        tpr = tp / (tp + fn) if tp + fn else 0.0
        fpr = fp / (fp + tn) if fp + tn else 0.0
        table[category] = {'tests': tp + fp + fn + tn, 'tp': tp, 'fn': fn, 'fp': fp, 'tn': tn,
                           'tpr': round(tpr, 3), 'fpr': round(fpr, 3), 'score': round(tpr - fpr, 3)}
    return table


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--benchmark-dir', required=True, type=Path)
    parser.add_argument('--clone', action='store_true', help=f'git clone {REPO_URL} when the directory is missing')
    parser.add_argument('--json', type=Path, help='write the scores to this file')
    args = parser.parse_args(argv)
    if not args.benchmark_dir.exists():
        if not args.clone:
            parser.error(f'{args.benchmark_dir} does not exist (use --clone to download the Benchmark)')
        subprocess.run(['git', 'clone', '--depth', '1', REPO_URL, str(args.benchmark_dir)], check=True)
    labels = expected(args.benchmark_dir)
    start = time.time()
    kg = RepoKnowledgeGraph.from_path(str(args.benchmark_dir / 'src' / 'main' / 'java'), extract_entities=True,
                                      index_nodes=False)
    elapsed = time.time() - start
    table = score(labels, detections(kg))
    print(f'Graph built in {elapsed:.0f} s for {len(labels)} labelled tests')
    print(f"{'category':14} {'tests':>5} {'TP':>5} {'FN':>5} {'FP':>5} {'TN':>5} {'TPR':>6} {'FPR':>6} {'score':>6}")
    covered = [c for c in table if table[c]['tests']]
    for category in covered:
        row = table[category]
        print(f"{category:14} {row['tests']:>5} {row['tp']:>5} {row['fn']:>5} {row['fp']:>5} {row['tn']:>5} "
              f"{row['tpr']:>6.2f} {row['fpr']:>6.2f} {row['score']:>6.2f}")
    average = sum(table[c]['score'] for c in covered) / len(covered) if covered else 0.0
    print(f'Average score over {len(covered)} categories: {average:.2f}')
    if args.json:
        args.json.write_text(json.dumps({'build_seconds': round(elapsed), 'categories': table,
                                         'average_score': round(average, 3)}, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
