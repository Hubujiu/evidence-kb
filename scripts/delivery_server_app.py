"""Validate the fixed delivery profile without creating an application.

Serve with uvicorn kb_platform.delivery:create_delivery_app --factory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb_platform.delivery import DeliveryConfigurationError, check_delivery_configuration  # noqa: E402


def main():
    if callable(getattr(sys.stdout, 'reconfigure', None)):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', required=True)
    parser.add_argument('--config')
    parser.add_argument('--providers')
    parser.add_argument('--run-root')
    parser.add_argument('--budget-batch')
    args = parser.parse_args()
    try:
        state = check_delivery_configuration(
            args.config, args.providers, args.run_root, budget_batch=args.budget_batch)
        print(json.dumps({'status': 'CONFIGURATION_VALID', **state}, ensure_ascii=False))
    except (DeliveryConfigurationError, ValueError) as exc:
        print(json.dumps({'status': 'CONFIGURATION_INVALID', 'error': str(exc)}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
