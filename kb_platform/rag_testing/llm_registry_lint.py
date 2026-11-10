import ast
import re


def compact(source):
    return re.sub(r'\s+', '', source)


def body(source, marker):
    start = source.find(marker)
    if start < 0:
        return ''
    start = source.find('{', start + len(marker)) + 1
    depth = 1
    for end in range(start, len(source)):
        depth += (source[end] == '{') - (source[end] == '}')
        if depth == 0:
            return source[start:end]
    return ''


def registry_source_violations(registry, executor, driver):
    errors = []
    current = compact(body(registry, 'fn current'))
    required = ['binding.service==self.identity', 'binding.generation==entry.generation',
                'state.routes.get(&entry.name)==Some(&binding.registration)',
                'entry.phase==RoutePhase::Active', 'state.phase==ServicePhase::Active']
    admit = compact(body(registry, 'pub fn admit'))
    if (not all(value in current for value in required)
            or 'pubfnadmit(self)' not in compact(registry)
            or 'self.inner.current(&state,self.snapshot.binding)?;' not in admit
            or admit.find('self.inner.current(') > admit.find('entry.active=entry.active.checked_add(1)')):
        errors.append('prepared admission must consume self and validate the exact current binding before increment')
    detach = compact(body(registry, 'pub fn detach'))
    if ('ifentry.generation!=generation{' not in detach or 'letremove=entry.active==0;' not in detach
            or 'drop(state);drop(retired);' not in detach):
        errors.append('detach must reject stale generation and retire zero-active payload outside the lock')
    drop = compact(body(registry, 'fn drop'))
    if 'entry.active-=1;ifentry.detached&&entry.active==0{' not in drop or 'drop(retired);' not in drop:
        errors.append('Admission drop must retire only its detached zero-active entry')
    adoption = compact(body(registry, 'pub fn adopt'))
    if ('entry.active!=0||entry.phase!=RoutePhase::Active' not in adoption
            or 'entry.generation=generation;entry.identity=identity;' not in adoption
            or 'state.issued.contains(&key)||!keys.insert(key)' not in adoption):
        errors.append('adoption must reject active calls and preserve fresh generation/identity validation')
    cleanup = compact(body(registry, 'pub fn begin_cleanup'))
    if ('ifentry.phase==RoutePhase::Cleaning{' not in cleanup
            or cleanup.find('ifentry.phase==RoutePhase::Cleaning') > cleanup.find('entry.phase=RoutePhase::Draining')
            or 'ifentry.active!=0{' not in cleanup):
        errors.append('cleanup must claim exclusively and refuse live admissions before cleaning')
    finish = compact(body(registry, 'pub fn finish_cleanup'))
    if ('ifentry.phase!=RoutePhase::Cleaning||entry.active!=0{' not in finish
            or 'entry.phase=RoutePhase::CleanupFailed;entry.cleanup_error=Some(error);None' not in finish
            or 'drop(state);drop(retired);' not in finish):
        errors.append('cleanup failure must retain the owned entry and destruction must occur outside the lock')
    close = compact(body(registry, 'pub fn begin_close'))
    register = compact(body(registry, 'pub fn register'))
    if ('state.phase=ServicePhase::Draining;' not in close
            or close.find('state.phase=ServicePhase::Draining;') > close.find('Ok(state.entries.iter()')
            or 'state.phase!=ServicePhase::Active||state.routes.contains_key(name)' not in register):
        errors.append('begin close must fence registration before returning its cleanup snapshot')
    begin = compact(body(executor, 'Command::CallBegin { prepared, .. } =>'))
    complete = compact(body(executor, 'Command::CallComplete { call, result, .. } =>'))
    release = compact(body(executor, 'Command::CallRelease { call, .. } =>'))
    if ('service.prepared.remove(&prepared)' not in begin
            or begin.find('service.prepared.remove(') > begin.find('prepared.prepared.admit()')
            or 'service.calls.get_mut(&call)' not in complete or '.remove(' in complete
            or 'call.completed=true;' not in complete or 'service.calls.remove(&call)' not in release):
        errors.append('external prepared ticket is consumed once and complete must not release its Admission')
    tree = ast.parse(driver)
    dispose = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == '_dispose')
    normalized = ast.unparse(dispose)
    fragments = ["self._registry.call('route_begin_cleanup'", 'handle.adapter.close()',
                 "self._registry.call('route_finish_cleanup', binding=binding, error=None)",
                 "del self._adapters[binding['registration']]"]
    positions = [normalized.find(value) for value in fragments]
    handlers = [node for node in ast.walk(dispose) if isinstance(node, ast.ExceptHandler)]
    if (any(value < 0 for value in positions) or positions != sorted(positions)
            or len(handlers) != 1 or any(isinstance(node, ast.Delete) for node in ast.walk(handlers[0]))
            or "error=type(error).__name__" not in ast.unparse(handlers[0])):
        errors.append('Python cleanup must use the native claim and retain its adapter on failed cleanup')
    return errors
