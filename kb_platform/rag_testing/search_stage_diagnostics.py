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
                summary = {'status': 'not_sampled_active_writers'}
                error.add_note(PREFIX + json.dumps({'channel': channel, 'line': trace.tb_lineno,
                                                   'metadata': summary, **detail},
                                                   sort_keys=True, ensure_ascii=True))
                return
            trace = trace.tb_next
    except BaseException:
        return


METADATA_LIMIT = 65536
METADATA_NAMES = ('meta.json', '.managed.json')


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _metadata_file(path):
    try:
        if path.name not in METADATA_NAMES or path.parent.name not in ('lexical', 'pages'):
            return {'status': 'not_allowlisted'}
        before = path.lstat()
        if _blocked(before) or not stat.S_ISREG(before.st_mode):
            return {'status': 'link_reparse_or_non_regular'}
        if before.st_size > METADATA_LIMIT:
            return {'status': 'oversized'}
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        try:
            stream = os.fdopen(descriptor, 'rb')
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before) or not stat.S_ISREG(opened.st_mode):
                return {'status': 'changed'}
            data = stream.read(METADATA_LIMIT + 1)
            after = os.fstat(stream.fileno())
        if len(data) > METADATA_LIMIT:
            return {'status': 'oversized'}
        if _identity(before) != _identity(after) or _identity(after) != _identity(path.lstat()):
            return {'status': 'changed'}
        value = json.loads(data)
        if path.name == 'meta.json':
            if type(value) is not dict or type(value.get('opstamp')) is not int or type(value.get('segments')) is not list:
                return {'status': 'unexpected_format'}
            return {'status': 'observed', 'opstamp': value['opstamp'], 'segment_count': len(value['segments'])}
        if type(value) is not list or any(type(item) is not str for item in value):
            return {'status': 'unexpected_format'}
        return {'status': 'observed', 'managed_count': len(value)}
    except (OSError, ValueError, TypeError, RecursionError):
        return {'status': 'unavailable'}


def metadata_counts(root, path, channel):
    try:
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
        if channel not in ('lexical', 'pages'):
            return {'status': 'not_allowlisted'}
        directory = path / channel
        info = directory.lstat()
        if _blocked(info) or not stat.S_ISDIR(info.st_mode):
            return {'status': 'link_reparse_or_non_directory'}
        return {'status': 'observed', 'channel': channel,
                'files': {name: _metadata_file(directory / name) for name in METADATA_NAMES}}
    except BaseException:
        return {'status': 'unavailable'}
