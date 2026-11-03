import argparse
import cProfile
import json
import os
from pathlib import Path
import pstats
import re
import subprocess
import sys
import time

from .formal_state_machines import registry_violations


def source_label(filename, root):
    path = Path(filename)
    if path.is_absolute():
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return '<external>/' + path.name
    return re.sub(r'[^A-Za-z0-9_.<> -]', '_', filename)


def safe_error(error, root):
    text = str(error).replace(str(root), '<repository>')
    return re.sub(r'(?:[A-Za-z]:[\\/]|/)[^\s\x27\x22,;]+', '<path>', text)


def profile_registry(root):
    revision = subprocess.run(['git', 'rev-parse', 'HEAD', 'HEAD^{tree}'], cwd=root,
                              check=True, capture_output=True, text=True, timeout=10).stdout.splitlines()
    if len(revision) != 2 or any(not re.fullmatch('[0-9a-f]{40}', value) for value in revision):
        raise ValueError('Expected exact Git head and tree')
    profiler = cProfile.Profile()
    wall = time.monotonic()
    cpu = time.process_time()
    errors = profiler.runcall(registry_violations, root)
    cpu = time.process_time() - cpu
    wall = time.monotonic() - wall
    rows = []
    for (filename, line, name), (primitive, calls, own, cumulative, callers) in pstats.Stats(profiler).stats.items():
        rows.append({'file': source_label(filename, root), 'line': line,
                     'function': re.sub(r'[^A-Za-z0-9_.<> -]', '_', name),
                     'primitive_calls': primitive, 'calls': calls,
                     'self_seconds': own, 'cumulative_seconds': cumulative})
    rows.sort(key=lambda row: (-row['cumulative_seconds'], row['file'], row['line'], row['function']))
    return {'head': revision[0], 'tree': revision[1], 'python': sys.version,
            'cpu_count': os.cpu_count(), 'platform': sys.platform,
            'wall_seconds': wall, 'process_seconds': cpu,
            'ordered_errors': [safe_error(error, root) for error in errors], 'profile': rows,
            'scope': 'One read-only registry call after failed checks; not a replacement or retry of contracts.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = profile_registry(Path.cwd().resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
    return 0 if not result['ordered_errors'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
