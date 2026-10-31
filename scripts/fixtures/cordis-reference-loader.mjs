import { pathToFileURL } from 'node:url'
import { resolve as resolvePath } from 'node:path'

export function resolve(specifier, context, nextResolve) {
  if (specifier === '@deepseek-ai/cosmokit') {
    const target = resolvePath(process.env.CORDIS_REFERENCE_ROOT, 'vendor/cosmokit/src/index.ts')
    return nextResolve(pathToFileURL(target).href, context)
  }
  return nextResolve(specifier, context)
}
