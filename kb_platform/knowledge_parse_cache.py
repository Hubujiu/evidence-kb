from collections import OrderedDict
from hashlib import sha256
import json
from threading import Lock


class ImmutableParseCache:
    def __init__(self, max_bytes=64 * 1024 * 1024, max_entries=8192):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = Lock()

    def get(self, key):
        with self._lock:
            item = self._entries.get(key)
            if item is None:
                return None
            self._entries.move_to_end(key)
        raw, checksum = item
        if sha256(raw).digest() != checksum:
            raise ValueError('IMMUTABLE_PARSE_CACHE_INTEGRITY')
        return json.loads(raw)

    def put(self, key, document):
        raw = json.dumps(document, ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(',', ':')).encode('utf-8')
        if len(raw) > self.max_bytes or self.max_entries < 1:
            return
        checksum = sha256(raw).digest()
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= len(previous[0])
            while self._entries and (self._bytes + len(raw) > self.max_bytes
                                     or len(self._entries) >= self.max_entries):
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= len(evicted[0])
            self._entries[key] = (raw, checksum)
            self._bytes += len(raw)
