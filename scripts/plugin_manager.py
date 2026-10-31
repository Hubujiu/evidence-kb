import argparse
import json
from pathlib import Path

from kb_platform.plugin_process import ProcessPackages


def main():
    parser = argparse.ArgumentParser(description='Install trusted local process plugins and list pinned packages.')
    parser.add_argument('--root', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    install = commands.add_parser('install')
    install.add_argument('source', type=Path)
    commands.add_parser('list')
    args = parser.parse_args()
    packages = ProcessPackages(args.root)
    result = packages.install(args.source) if args.command == 'install' else packages.installed()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
