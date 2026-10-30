import assert from 'node:assert/strict'
import { readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { createHash } from 'node:crypto'

const [sourceRoot, output, lockFile] = process.argv.slice(2)
const lockText = (await readFile(lockFile, 'utf8')).replaceAll('\r\n', '\n')
const lock = JSON.parse(lockText)
assert.equal(process.version, lock.node_version)
const { Context, FiberState } = await import(pathToFileURL(resolve(sourceRoot, 'vendor/cordis/src/index.ts')))
const scenarios = []

{
  const root = new Context()
  const events = []
  let alive = true
  await root.plugin(ctx => {
    events.push('provider.start')
    ctx.provide('fixture', { read: () => alive ? 'open' : 'closed' })
    return () => { alive = false; events.push('provider.stop') }
  })
  await root.inject(['fixture'], ctx => {
    events.push('consumer.start', `read:${ctx.fixture.read()}`)
    return () => { events.push(`read:${ctx.fixture.read()}`, 'consumer.stop') }
  })
  await root.fiber.dispose()
  assert.deepEqual(events, ['provider.start', 'consumer.start', 'read:open', 'read:open', 'consumer.stop', 'provider.stop'])
  scenarios.push({ id: 'dependency_cleanup_order', events })
}

{
  const root = new Context()
  const events = []
  const consumer = await root.inject(['fixture'], ctx => {
    events.push(`consumer.start:${ctx.fixture}`)
    return () => events.push('consumer.stop')
  })
  assert.equal(consumer.state, FiberState.PENDING)
  events.push('consumer.pending')
  const provider = await root.plugin(ctx => {
    events.push('provider.start')
    ctx.provide('fixture', 'ready')
    return () => events.push('provider.stop')
  })
  await consumer.await()
  await provider.dispose()
  assert.equal(consumer.state, FiberState.PENDING)
  events.push('consumer.pending_again')
  await root.fiber.dispose()
  scenarios.push({ id: 'dynamic_dependency_appearance_and_removal', events })
}

{
  const root = new Context()
  const events = []
  await root.plugin(ctx => {
    ctx.provide('fixture', 'parent')
    events.push('parent.start')
    return () => events.push('parent.stop')
  })
  let isolated
  const child = await root.plugin(ctx => {
    isolated = ctx.isolate('fixture')
    isolated.provide('fixture', 'child')
    events.push('child.start')
    return () => events.push('child.stop')
  })
  const values = [root.fixture, isolated.fixture]
  await child.dispose()
  values.push(root.fixture)
  events.push('parent.still_available')
  await root.fiber.dispose()
  scenarios.push({ id: 'scope_override_and_borrowed_parent', values, events })
}

{
  const root = new Context()
  const events = []
  const owner = await root.plugin(ctx => {
    ctx.on('fixture', value => { events.push(`a:${value}`) })
    ctx.on('fixture', value => { events.push(`b:${value}`) })
  })
  root.emit('fixture', 2)
  await owner.dispose()
  root.emit('fixture', 3)
  await root.fiber.dispose()
  scenarios.push({ id: 'emit_order_and_listener_ownership', events })
}

{
  const root = new Context()
  const events = []
  const gate = Promise.withResolvers()
  root.on('fixture', async () => {
    events.push('a.start')
    await gate.promise
    events.push('a.done')
  })
  root.on('fixture', async () => {
    events.push('b.start', 'b.done')
    gate.resolve()
  })
  await root.parallel('fixture')
  await root.fiber.dispose()
  scenarios.push({ id: 'parallel_waits_for_every_listener', events,
    required_before: [['a.start', 'a.done'], ['b.start', 'b.done'], ['a.start', 'b.start'], ['b.start', 'a.done']] })
}

for (const mode of ['serial', 'bail']) {
  const root = new Context()
  const events = []
  root.on('fixture', () => { events.push('false'); return false })
  root.on('fixture', () => { events.push('none'); return null })
  root.on('fixture', () => { events.push('zero'); return 0 })
  root.on('fixture', () => { events.push('unreachable'); return 9 })
  const result = await root[mode]('fixture')
  assert.equal(result, 0)
  assert.deepEqual(events, ['false', 'none', 'zero'])
  await root.fiber.dispose()
  scenarios.push({ id: `${mode}_strict_bail_predicate`, events, result })
}

{
  const root = new Context()
  const events = []
  root.on('fixture', (value, next) => {
    events.push('outer.before')
    const result = next()
    events.push('outer.after')
    return result + 1
  })
  root.on('fixture', (value, next) => {
    events.push('inner.before')
    const result = next()
    events.push('inner.after')
    return result * 2
  })
  const result = root.waterfall('fixture', 3, () => 3)
  assert.equal(result, 7)
  await root.fiber.dispose()
  scenarios.push({ id: 'waterfall_middleware', events, result })
}

{
  const root = new Context()
  root.on('fixture', (value, next) => { next(); return next() })
  let rejected = false
  try {
    root.waterfall('fixture', 1, () => 1)
  } catch (error) {
    rejected = error.message === 'next() called multiple times'
  }
  assert.equal(rejected, true)
  await root.fiber.dispose()
  scenarios.push({ id: 'waterfall_repeated_next', rejected })
}

const result = {
  schema: 'cordis-reference-traces/1',
  source: { repository: lock.repository, commit: lock.commit, packages: lock.packages,
    node_version: process.version, lock_text_sha256: createHash('sha256').update(lockText).digest('hex') },
  scenarios,
  intentional_differences: [
    { id: 'cleanup_error_retention', reference: 'reference cleanup contains errors and releases dependencies',
      target: 'CleanupFailed retains unfinished resources and required dependencies until explicit successful retry' },
    { id: 'emit_observer_errors', reference: 'emit propagates synchronous listener errors',
      target: 'emit records observer errors and returns their tuple while continuing peers' },
    { id: 'synchronous_python_bridge', reference: 'JavaScript promises permit async lifecycle and callbacks',
      target: 'Python mount/dispose and callback completion are synchronous; cancellation cannot preempt blocking Python' },
    { id: 'scope_security', reference: 'JavaScript Proxy and isolation labels provide contextual visibility',
      target: 'explicit service handles, scopes and registration identities provide visibility and ownership without malicious-plugin sandboxing' },
  ],
}
await writeFile(output, JSON.stringify(result, null, 2) + '\n', { flag: 'wx' })
console.log(JSON.stringify({ decision: 'PASS', scenarios: scenarios.length, output }))
