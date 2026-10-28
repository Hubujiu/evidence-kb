"""Inspect a provider latch or acknowledge an exact checkpoint after external repair.

Never probes a provider, modifies a physical request, or resets a budget. The
private control directory is recorded in the delivery runtime manifest.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb_platform.provider_admission import ProviderAdmission  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'acknowledge'))
    parser.add_argument('--directory', required=True)
    parser.add_argument('--expected-checkpoint')
    parser.add_argument('--evidence-reference')
    args = parser.parse_args(argv)
    try:
        control = ProviderAdmission.from_directory(Path(args.directory))
        if args.action == 'inspect':
            result = control.snapshot()
        else:
            if not args.expected_checkpoint or not args.evidence_reference:
                parser.error('acknowledge requires --expected-checkpoint and --evidence-reference')
            result = control.acknowledge(expected_checkpoint=args.expected_checkpoint,
                                         evidence_reference=args.evidence_reference)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'status': 'CONTROL_REJECTED', 'reason': 'INVALID_OR_CHANGED_PROVIDER_ADMISSION',
                          'provider_probe_performed': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
