from .test_outcomes import passed_contract_run

import argparse
import json
from pathlib import Path
import sys
import unittest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from . import foundation_consumer_integration, foundation_knowledge_integration, foundation_transport_integration

    suite = unittest.TestSuite()
    for module in (foundation_consumer_integration, foundation_knowledge_integration,
                   foundation_transport_integration):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    planned = suite.countTestCases()
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    passed = passed_contract_run(result, planned)
    report = {'decision': 'PASS' if passed else 'FAIL', 'planned': planned,
        'total': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'expected_failures': len(result.expectedFailures),
        'new_model_calls': 0, 'new_embedding_calls': 0, 'new_judge_calls': 0,
        'scope': 'Standalone foundation consumer, controlled localhost OpenAI/Anthropic/S3 SDK wire, '
                 'knowledge application composition and send-time authorization; current native library; '
                 'synthetic component fixtures, no live provider or production RAG quality claim'}
    (args.output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
