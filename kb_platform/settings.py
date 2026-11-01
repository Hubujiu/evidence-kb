import json
import os
from dataclasses import dataclass
from pathlib import Path

from .session_identity import Actor


@dataclass(frozen=True)
class Settings:
    storage_root: Path
    identities_file: Path
    storage_file: Path | None = None

    @classmethod
    def from_environment(cls):
        if any(os.getenv(name) for name in ('KB_DSN', 'KB_SEARCH_URL', 'KB_SEARCH_BACKEND',
                                             'KB_SEARCH_CHANNELS_CONFIG', 'KB_DATABASES_CONFIG')):
            raise ValueError('STORAGE_CONFIGURATION_REQUIRED_USE_KB_STORAGE_CONFIG')
        return cls(
            Path(os.getenv('KB_STORAGE_ROOT', '.local/store')).resolve(),
            Path(os.getenv('KB_IDENTITIES_FILE', '.local/identities.json')).resolve(),
            Path(os.getenv('KB_STORAGE_CONFIG', '.local/storage.json')).resolve(),
        )

    def identities(self):
        if not self.identities_file.is_file():
            raise RuntimeError("Run uv run python scripts/bootstrap.py to create local identities.")
        entries = json.loads(self.identities_file.read_text(encoding="utf-8"))
        if not entries or any(len(token) < 32 for token in entries):
            raise RuntimeError("Identity tokens must have at least 32 characters.")
        return {token: Actor(**value) for token, value in entries.items()}
