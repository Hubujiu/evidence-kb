import re


def compact(source):
    return re.sub(r'\s+', '', source)


def body(source, marker):
    start = source.find(marker)
    if start < 0:
        return ''
    start = source.find('{', start) + 1
    depth = 1
    for end in range(start, len(source)):
        depth += (source[end] == '{') - (source[end] == '}')
        if depth == 0:
            return source[start:end]
    return ''


def stream_source_violations(service, assembler):
    errors = []
    next_body = body(service, 'pub async fn next(')
    terminal = body(next_body, 'if self.assembler.finished()')
    expected = '''
        if let Ok(observed) = self.observed_usage.lock() {
            self.terminal_usage = observed.clone();
        }
        self.done = true;
        self.receiver.close();
    '''
    flow = '''let event = match self.assembler.push(event.clone()) {
        Ok(()) => event,
        Err(error) => {
            self.cancellation.cancel();
            self.assembler.fail(error, false)
        }
    };'''
    if (compact(terminal) != compact(expected) or compact(flow) not in compact(next_body)
            or next_body.count('self.terminal_usage =') != 1
            or next_body.find('self.assembler.fail(') > next_body.find('if self.assembler.finished()')
            or not compact(next_body).endswith('Some(event)')
            or 'terminal_usage: None,' not in service):
        errors.append('common terminal publication must freeze the observed usage snapshot after validation')
    response = compact(body(service, 'pub fn response('))
    projection = 'ifletSome(usage)=&self.terminal_usage{response.usage=Some(usage.clone());}'
    if projection not in response or 'unwrap_or_default' in response or response.count('response.usage=') != 1:
        errors.append('response must replace with the terminal snapshot, never fabricate or add usage')
    close = compact(body(service, 'pub async fn close('))
    if ('tokio::time::timeout(timeout,task).await.map_err(' not in close
            or '})?;self.task=None;' not in close or '.abort(' in service
            or 'drop(owner);' in next_body
            or ').await;drop(owner);' not in compact(service)):
        errors.append('terminal publication must not release or abort the actual provider task owner')
    push = compact(body(assembler, 'pub fn push('))
    checked = compact(body(assembler, 'fn push_checked('))
    if ('letmutnext=self.clone();next.push_checked(event)?;*self=next;' not in push
            or 'ifserde_json::to_vec(self).map_or(true,|v|v.len()>MAX_CONTENT_BYTES)' not in checked):
        errors.append('content admission must retain clone-validate-commit and the byte limit')
    return errors
