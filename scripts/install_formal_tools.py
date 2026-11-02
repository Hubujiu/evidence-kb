from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description='Explicitly fetch the locked official TLC release; never run during validation.')
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    lock = json.loads((ROOT / 'formal/toolchain-v1.json').read_text('utf-8'))
    destination = args.destination or ROOT / lock['default_jar']
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        data = destination.read_bytes()
        if hashlib.sha256(data).hexdigest() != lock['sha256'] or len(data) != lock['size_bytes']:
            raise SystemExit('Existing TLC artifact does not match the locked release; refusing replacement')
        print(f'Already verified {destination}')
        return 0
    with urllib.request.urlopen(lock['url'], timeout=60) as response:
        data = response.read(lock['size_bytes'] + 1)
    if hashlib.sha256(data).hexdigest() != lock['sha256'] or len(data) != lock['size_bytes']:
        raise SystemExit('Downloaded TLC artifact does not match the locked release')
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        os.link(temporary, destination)
    finally:
        temporary.unlink()
    print(f'Installed verified {lock["release"]}: {destination}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
