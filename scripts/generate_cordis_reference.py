import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description='Run pinned Cordis sources independently and freeze observed traces.')
    parser.add_argument('--workdir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--download', action='store_true', help='Fetch missing source files from their fixed commit.')
    args = parser.parse_args()
    output, workdir = args.output.resolve(), args.workdir.resolve()
    if output.exists():
        parser.error('--output must not exist; earlier reference observations remain read-only')
    lockfile = ROOT / 'config/plugin-runtime/cordis-reference-lock.json'
    lock = json.loads(lockfile.read_text('utf-8'))
    node = shutil.which('node')
    if node is None:
        parser.error('the exact Node runtime named in the lock is required')
    version = subprocess.run([node, '--version'], check=True, capture_output=True, text=True).stdout.strip()
    if version != lock['node_version']:
        parser.error(f"Node {lock['node_version']} is required; found {version}")
    for name, expected in lock['harness_text_sha256'].items():
        actual = sha256((ROOT / name).read_text('utf-8').encode()).hexdigest()
        if actual != expected:
            parser.error(f'reference harness differs from its lock: {name}')
    for item in lock['files']:
        target = workdir / item['path']
        if not target.is_file():
            if not args.download:
                parser.error(f"missing locked source: {item['path']}; --download permits fetching it")
            url = f"https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/{lock['commit']}/{item['path']}"
            with urlopen(url, timeout=60) as response:
                data = response.read()
            if sha256(data).hexdigest() != item['sha256']:
                parser.error(f"download hash mismatch: {item['path']}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if sha256(target.read_bytes()).hexdigest() != item['sha256']:
            parser.error(f"locked source was modified: {item['path']}")
    environment = {key: value for key, value in os.environ.items()
        if key not in {'NODE_OPTIONS', 'NODE_PATH'} and not key.startswith(
            ('KB_', 'EVIDENCE_KB_', 'OPENAI_', 'ANTHROPIC_', 'GOOGLE_', 'AZURE_'))}
    environment['CORDIS_REFERENCE_ROOT'] = str(workdir)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([node, '--experimental-transform-types', '--loader',
        (ROOT / 'scripts/fixtures/cordis-reference-loader.mjs').as_uri(),
        str(ROOT / 'scripts/fixtures/cordis-reference-runner.mjs'), str(workdir), str(output), str(lockfile)],
        cwd=ROOT, env=environment, check=False)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
