import argparse
import json
from pathlib import Path
import sys
import unittest


def case_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from case_ids(item)
        else:
            yield item.id()


def integration_suite():
    from . import (acceptance_workflow_integration, foundation_consumer_integration,
                   foundation_knowledge_integration, foundation_transport_integration, http_lifespan_integration,
                   receipt_process_integration, check_process_owner_integration)
    from .delivery_status_contracts import live_suite as delivery_status_suite
    from .browser_owner_contracts import ASSET_CASE_ID, live_suite as browser_owner_suite

    suite = unittest.TestSuite()
    for module in (foundation_consumer_integration, foundation_knowledge_integration,
                   foundation_transport_integration, http_lifespan_integration, receipt_process_integration,
                   acceptance_workflow_integration, check_process_owner_integration):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    suite.addTests(delivery_status_suite())
    suite.addTest(browser_owner_suite())
    if list(case_ids(suite)).count(ASSET_CASE_ID) != 1:
        raise ValueError('Controlled foundation stage requires exactly one required browser asset case')
    return suite


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    suite = integration_suite()
    planned = suite.countTestCases()
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    passed = (result.wasSuccessful() and result.testsRun == planned > 0
              and not result.skipped and not result.expectedFailures)
    report = {'decision': 'PASS' if passed else 'FAIL', 'planned': planned,
        'total': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'expected_failures': len(result.expectedFailures),
        'new_model_calls': 0, 'new_embedding_calls': 0, 'new_judge_calls': 0,
        'scope': 'Standalone foundation consumer, controlled localhost OpenAI/Anthropic/S3 SDK wire, '
                 'knowledge composition, send-time authorization, ASGI delivery status and public browser assets; '
                 'file receipt process/crash exclusion and acceptance preparation owner exclusion; current native library; '
                 'HTTP lifespan and never-started cleanup; synthetic component fixtures, no live provider or production RAG quality claim'}
    (args.output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
