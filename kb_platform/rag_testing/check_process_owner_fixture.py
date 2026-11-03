import json
import os
from pathlib import Path
import subprocess
import sys
import time

from scripts.check_process_owner_witness import own_birth


def main():
    directory = Path(sys.argv[1])
    role = sys.argv[2]
    receipt = directory / f'{role}.json'
    staging = receipt.with_suffix('.tmp')
    staging.write_text(json.dumps({'pid': os.getpid(), 'birth': own_birth()}), 'utf-8')
    staging.replace(receipt)
    if role == 'grandchild':
        time.sleep(12)
        return 0
    subprocess.Popen([sys.executable, '-B', '-m',
        'kb_platform.rag_testing.check_process_owner_fixture', str(directory), 'grandchild'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=os.name == 'posix', close_fds=True)
    deadline = time.monotonic() + 8
    while not (directory / 'release').exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if sys.argv[3] == 'exit':
        return 0
    time.sleep(12)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
