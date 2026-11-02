from html.parser import HTMLParser
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]
ENTRIES = {'kb_platform/console.html': ('kb_platform/console.mjs', '/console.mjs')}


class BrowserMarkup(HTMLParser):
    def __init__(self, module_url):
        super().__init__(convert_charrefs=True)
        self.module_url, self.scripts, self.in_script, self.errors = module_url, 0, False, []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if len(values) != len(attrs):
            self.errors.append('Duplicate HTML attributes are not permitted')
        for name, value in attrs:
            if name.startswith('on') or name == 'srcdoc':
                self.errors.append('Inline event handlers and srcdoc bypass the mapped owner')
            if value and re.sub(r'[\x00-\x20]', '', value).lower().startswith('javascript:'):
                self.errors.append('JavaScript URLs bypass the mapped owner')
        if tag in {'iframe', 'object', 'embed', 'applet', 'base'}:
            self.errors.append('Embedded execution or base URL changes bypass the mapped owner')
        if tag == 'meta' and values.get('http-equiv', '').lower() == 'refresh':
            self.errors.append('Markup refresh bypasses the mapped owner')
        if tag == 'script':
            self.scripts += 1
            self.in_script = True
            if values != {'type': 'module', 'src': self.module_url}:
                self.errors.append('Only the single mapped module script is permitted')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)
        if tag == 'script':
            self.errors.append('The mapped script requires an explicit closing tag')

    def handle_endtag(self, tag):
        if tag == 'script':
            if not self.in_script:
                self.errors.append('Unexpected script closing tag')
            self.in_script = False

    def handle_data(self, data):
        if self.in_script and data.strip():
            self.errors.append('Inline script text bypasses the mapped owner')


def markup_violations(source, module_url):
    parser = BrowserMarkup(module_url)
    parser.feed(source)
    parser.close()
    if parser.scripts != 1 or parser.in_script:
        parser.errors.append('Exactly one complete mapped module script is required')
    return sorted(set(parser.errors))


def placement_violations(root):
    errors = []
    for entrypoint, (module, url) in ENTRIES.items():
        for relative in (entrypoint, module):
            path = root / relative
            if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                errors.append(f'{relative}: mapped browser source is missing or escaping')
        if (root / entrypoint).is_file():
            errors.extend(f'{entrypoint}: {error}' for error in
                          markup_violations((root / entrypoint).read_text('utf-8'), url))
    return errors


def main():
    errors = placement_violations(ROOT)
    node = shutil.which('node')
    results = []
    version, control = None, None
    if node is None:
        errors.append('Node is required for actual ECMAScript module syntax checking')
    else:
        version = subprocess.run([node, '--version'], check=True, capture_output=True,
                                 text=True, timeout=30).stdout.strip()
        for module, _ in ENTRIES.values():
            result = subprocess.run([node, '--check', str(ROOT / module)], check=False,
                                    capture_output=True, text=True, timeout=30)
            results.append({'module': module, 'returncode': result.returncode,
                            'source_sha256': sha256((ROOT / module).read_bytes()).hexdigest(),
                            'stdout': result.stdout, 'stderr': result.stderr})
            if result.returncode:
                errors.append(f'{module}: Node ECMAScript module syntax check failed')
        invalid = subprocess.run([node, '--input-type=module', '--check'], input='export const = ;',
                                 check=False, capture_output=True, text=True, timeout=30)
        control = {'returncode': invalid.returncode, 'stderr': invalid.stderr}
        if invalid.returncode == 0 or 'SyntaxError' not in invalid.stderr:
            errors.append('Node must reject the seeded invalid ECMAScript module')
    print(json.dumps({'decision': 'FAIL' if errors else 'PASS', 'violations': errors,
                      'node_version': version, 'syntax_checks': results, 'negative_control': control,
                      'scope': 'Syntax, placement and separate source-review fingerprints; '
                      'not semantic JavaScript verification or an HTML security sandbox.'}))
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
