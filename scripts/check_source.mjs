import { createRequire } from 'node:module';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, extname, join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(join(root, 'web/package.json'));
const { parse } = require('@babel/parser');
const postcss = require('postcss');
const { parse: parseHtml } = await import(pathToFileURL(require.resolve('parse5')).href);

export function rustComments(source) {
  const comments = [];
  const token = /(?:br|cr|r)(#*)"|(?:b|c)?"|'(?:\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|[^])|[^'\\\r\n])'|\/\/[^\r\n]*|\/\*/gy;
  let position = 0;
  while (position < source.length) {
    token.lastIndex = position;
    const match = token.exec(source);
    if (!match) { position++; continue; }
    const start = position;
    position = token.lastIndex;
    if (match[1] !== undefined) {
      const close = '"' + match[1];
      const end = source.indexOf(close, position);
      if (end < 0) throw new Error('Unterminated Rust raw string');
      position = end + close.length;
    } else if (match[0].endsWith('"')) {
      while (position < source.length && source[position] !== '"') {
        position += source[position] === '\\' ? 2 : 1;
      }
      if (position === source.length) throw new Error('Unterminated Rust string');
      position++;
    } else if (match[0] === '/*') {
      let depth = 1;
      while (position < source.length && depth) {
        const pair = source.slice(position, position + 2);
        if (pair === '/*') { depth++; position += 2; }
        else if (pair === '*/') { depth--; position += 2; }
        else position++;
      }
      if (depth) throw new Error('Unterminated Rust comment');
      comments.push({ start, end: position });
    } else if (match[0].startsWith('//')) comments.push({ start, end: position });
  }
  return comments;
}

export function sourceComments(source, extension) {
  if (extension === '.rs') return rustComments(source);
  if (extension === '.css') {
    const comments = [];
    postcss.parse(source).walkComments(node => comments.push({
      start: node.source.start.offset, end: node.source.end.offset,
    }));
    return comments;
  }
  if (extension === '.html') {
    const comments = [];
    function visit(node) {
      const location = node.sourceCodeLocation;
      if (node.nodeName === '#comment') comments.push({ start: location.startOffset, end: location.endOffset });
      if (['script', 'style'].includes(node.tagName) && location?.endTag) {
        const start = location.startTag.endOffset;
        const type = node.attrs.find(attribute => attribute.name === 'type')?.value;
        if (node.tagName === 'style' || !type || ['module', 'text/javascript', 'application/javascript'].includes(type)) {
          comments.push(...sourceComments(source.slice(start, location.endTag.startOffset), node.tagName === 'style' ? '.css' : '.js')
            .map(comment => ({ start: start + comment.start, end: start + comment.end })));
        }
      }
      for (const child of node.childNodes ?? []) visit(child);
      if (node.content) visit(node.content);
    }
    visit(parseHtml(source, { sourceCodeLocationInfo: true }));
    return comments;
  }
  return parse(source, {
    sourceType: 'unambiguous', plugins: ['jsx', ...(extension.startsWith('.ts') ? ['typescript'] : [])],
  }).comments.map(({ start, end }) => ({ start, end }));
}

export function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    if (['node_modules', 'dist', '__pycache__', 'target', '.git', '.artifacts'].includes(entry.name)) return [];
    const path = join(directory, entry.name);
    return entry.isDirectory() ? sourceFiles(path) : /\.(?:[cm]?js|tsx?|css|rs|html)$/.test(entry.name) ? [path] : [];
  });
}

export function checkSources() {
  let failed = false;
  const files = ['web', 'e2e', 'scripts', 'rust', 'examples', 'kb_platform'].flatMap(directory => sourceFiles(join(root, directory)));
  for (const path of files) {
    try {
      const source = readFileSync(path, 'utf8');
      for (const comment of sourceComments(source, extname(path))) {
        console.error(`${relative(root, path)}:${source.slice(0, comment.start).split('\n').length}: code comments are not allowed`);
        failed = true;
      }
    } catch (error) {
      console.error(`${relative(root, path)}: ${error.message}`);
      failed = true;
    }
  }
  console.log(`Source comment check ${failed ? 'FAIL' : 'PASS'} (${files.length} files)`);
  return failed ? 1 : 0;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) process.exitCode = checkSources();
