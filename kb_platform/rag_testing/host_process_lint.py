import ast
import re


PATHS = ('kb_platform/host_worker.py', 'kb_platform/host_protocol.py', 'kb_platform/plugin_process.py',
         'rust/plugin-host/src/lib.rs', 'rust/plugin-host/src/process_plugin.rs',
         'rust/plugin-host/src/process_tree.rs', 'rust/plugin-host/src/process_tree/unix.rs',
         'rust/plugin-host/src/process_tree/windows.rs')


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


def function(source, name):
    tree = ast.parse(source)
    return next((ast.unparse(node) for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == name), '')


def ordered(source, *fragments):
    positions = [source.find(fragment) for fragment in fragments]
    return all(position >= 0 for position in positions) and positions == sorted(set(positions))


def host_source_violations(sources):
    worker, protocol, transport, host, sdk, tree, unix, windows = (sources[path] for path in PATHS)
    errors = []
    run, shutdown, execute = (function(worker, name) for name in ('_run', '_shutdown', '_execute'))
    if (not ordered(run, 'finally:', 'self._finishing = True', 'self._stop_done.wait()', 'self._application.close()')
            or not ordered(shutdown, 'self._stop_done.clear()', 'stop()', 'finally:',
                           'self._stop_done.set()', 'self._settled.wait()', "return {'stopped': True}")
            or not ordered(execute, 'result = self._run()', 'self.connection.reply(', 'finally:', 'self._settled.set()')):
        errors.append('application shutdown must wait for stop and actual run/close settlement')
    request, receive, send = (function(protocol, name) for name in ('request', 'receive', 'send'))
    if (not ordered(request, 'self._next_id += 1', 'self._pending[identity] = future', 'self.send(', 'future.result()')
            or "future = self._pending.get(frame['id'])" not in receive
            or 'if future is None or future.done():' not in receive
            or 'with self._write_lock:' not in send or 'self.fail(exc)' not in send):
        errors.append('native requests require unique allocation and exact response correlation, not monotone wire order')
    write, request, close = (function(transport, name) for name in ('_write', 'request', 'close'))
    if not ordered(write, 'write.sent = True', 'self._record(write.request, write.status)', 'self._process.stdin.write(payload)'):
        errors.append('write intent must precede journal and physical bytes without asserting dispatch')
    if (not ordered(request, 'sent = write.sent', 'if not sent:', "self._record(request, 'UNKNOWN'", 'self._terminate()',
                    "raise PluginError('PROCESS_RESULT_UNKNOWN')")
            or request.count('self._enqueue(write)') != 1):
        errors.append('sent-intent failure must remain unknown without automatic replay')
    if not ordered(close, 'self._draining = True', 'if self._pending:', 'self._terminate()',
                   'self._stop_writer()', 'self._supervisor.join(', 'self._reader.join(', 'self._closed = True'):
        errors.append('transport close must fence requests and join real I/O before publishing closed')
    host_run = compact(body(host, 'pub fn run('))
    if ('whileresult.is_none()||!stopped' not in host_run or 'if!roots.is_empty()' not in host_run
            or 'worker.reap(shutdown.unwrap())?;' not in host_run
            or '(Ok(_),Err(error))=>Err(HostError::new("worker_cleanup",error.to_string()))' not in host_run):
        errors.append('host success must require both replies, collected roots, reap and final cleanup result')
    serve = compact(body(sdk, 'pub fn serve<'))
    if (not ordered(serve, 'call.cancelled.store(true,Ordering::Release);', 'letjoined=join(&mutworkers);',
                    'letclosed=plugin.close();', 'result?;', 'joined?;', 'closed.map(|()|Value::Null)')
            or 'active.lock().unwrap().contains_key(id)||!seen.insert(id)' not in serve
            or 'request["identity"][key]!=identity[key]' not in serve
            or 'self.order.len()>1024' not in compact(body(sdk, 'fn insert('))):
        errors.append('SDK shutdown must join actual handlers before close and preserve bounded identity guards')
    if ('self.child.try_wait()?.is_some()&&self.group.is_empty()?' not in compact(body(tree, 'pub fn shutdown('))
            or 'returntermination;' not in compact(body(tree, 'pub fn shutdown('))
            or 'libc::killpg(group,libc::SIGKILL)' not in compact(body(unix, 'pub fn terminate('))
            or 'Ok(true)' not in compact(body(unix, 'pub fn is_empty('))
            or 'TerminateJobObject(' not in body(windows, 'pub fn terminate(')
            or 'accounting.ActiveProcesses==0' not in compact(body(windows, 'pub fn is_empty('))):
        errors.append('process-tree observations must preserve reviewed platform-specific cleanup boundary')
    if ('len(payload) > MAX_FRAME' not in function(protocol, 'encode_frame')
            or 'len(payload) > MAX_MESSAGE' not in request
            or 'bytes.len()+1>MAX_FRAME_BYTES' not in compact(body(sdk, 'fn send('))):
        errors.append('protocol frame bounds must remain enforced')
    return errors
