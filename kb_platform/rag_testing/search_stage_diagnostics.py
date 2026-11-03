from collections import deque
import json
import os
from pathlib import Path
import stat

from kb_platform.embedded_search import EmbeddedSearch


PREFIX = 'embedded-search-stage-first-failure: '
LIMIT = 64


def _blocked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _owned_snapshot(root, path):
    root, path = Path(os.path.abspath(root)), Path(os.path.abspath(path))
    if not path.is_relative_to(root):
        return {'status': 'outside_owned_root'}
    current = root
    for part in (None, *path.relative_to(root).parts):
        if part is not None:
            current /= part
        info = current.lstat()
        if _blocked(info) or not stat.S_ISDIR(info.st_mode):
            return {'status': 'link_reparse_or_non_directory'}
    entries, pending, truncated = [], deque([(path, 0)]), False
    while pending:
        directory, depth = pending.popleft()
        if _blocked(directory.lstat()):
            continue
        with os.scandir(directory) as children:
            for child in children:
                if len(entries) == LIMIT:
                    truncated = True
                    break
                info = child.stat(follow_symlinks=False)
                relative = Path(child.path).relative_to(root).as_posix()
                entries.append({'name': relative[:512], 'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
                    'mode': stat.S_IFMT(info.st_mode), 'attributes': getattr(info, 'st_file_attributes', 0),
                    'blocked': _blocked(info)})
                if depth < 1 and stat.S_ISDIR(info.st_mode) and not _blocked(info):
                    pending.append((Path(child.path), depth + 1))
        if truncated:
            break
    return {'status': 'observed', 'staging': path.relative_to(root).as_posix(),
            'entries': sorted(entries, key=lambda item: item['name']), 'truncated': truncated}


def note_stage_failure(error, root):
    try:
        if any(isinstance(note, str) and note.startswith(PREFIX) for note in getattr(error, '__notes__', ())):
            return
        trace = error.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code is EmbeddedSearch.stage.__code__:
                values = trace.tb_frame.f_locals
                writers, writer = values.get('writers'), values.get('writer')
                channel = next((name for name, value in writers.items()
                    if name in ('lexical', 'pages') and value is writer), 'unknown') if type(writers) is dict else 'unknown'
                path = values.get('path')
                detail = _owned_snapshot(root, path) if isinstance(path, Path) else {'status': 'no_staging_path'}
                error.add_note(PREFIX + json.dumps({'channel': channel, 'line': trace.tb_lineno, **detail},
                                                   sort_keys=True, ensure_ascii=True))
                return
            trace = trace.tb_next
    except BaseException:
        return
