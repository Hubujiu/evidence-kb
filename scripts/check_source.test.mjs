import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { sourceComments, sourceFiles } from './check_source.mjs';

for (const [extension, source, expected] of [
  ['.ts', 'const url = "https://example.com"; // remove', ['// remove']],
  ['.tsx', 'const view = <div>https://example.com{/* remove */}</div>;', ['/* remove */']],
  ['.js', 'const matcher = /https?:\\/\\//; const text = `/* literal */ ${1 /* remove */}`;', ['/* remove */']],
  ['.css', 'a { content: "/* literal */"; }/* remove */b {}', ['/* remove */']],
  ['.html', '<!-- remove --><script>const url="https://a"; /* js */</script><style>a{content:"/* literal */"}/* css */</style>', ['<!-- remove -->', '/* js */', '/* css */']],
  ['.rs', 'let a = r###"/* literal */"###; let b = br#"// literal"#; let c = cr"// literal"; /* outer /* inner */ end */', ['/* outer /* inner */ end */']],
  ['.rs', String.raw`fn f<'a>(x: &'a str) { let q = '\''; let c = b'/'; let s = "escaped \\"; } // remove`, ['// remove']],
  ['.rs', String.raw`let s = "escaped \" // literal"; // remove`, ['// remove']],
]) {
  test(`${extension} preserves literals and identifies comment spans: ${source}`, () => {
    assert.deepEqual(sourceComments(source, extension).map(({ start, end }) => source.slice(start, end)), expected);
  });
}

test('malformed sources fail instead of silently bypassing lint', () => {
  for (const [source, extension] of [['const =', '.js'], ['a {', '.css'], ['/*', '.rs'], ['r#"', '.rs']]) {
    assert.throws(() => sourceComments(source, extension));
  }
});

test('source discovery includes application files and excludes generated browser artifacts', () => {
  const root = mkdtempSync(join(tmpdir(), 'source-discovery-'));
  try {
    writeFileSync(join(root, 'app.ts'), 'export const value = 1;');
    for (const directory of ['node_modules', 'dist', '.artifacts']) {
      mkdirSync(join(root, directory));
      writeFileSync(join(root, directory, 'generated.js'), '// generated');
    }
    assert.deepEqual(sourceFiles(root), [join(root, 'app.ts')]);
  } finally {
    rmSync(root, { recursive: true });
  }
});
