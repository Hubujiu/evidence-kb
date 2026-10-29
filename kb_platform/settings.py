import json
import os
from dataclasses import dataclass
from pathlib import Path

from .session_identity import Actor


@dataclass(frozen=True)
class Settings:
    dsn: str
    search_url: str
    storage_root: Path
    identities_file: Path
    search_backend: str = 'postgres'

    @classmethod
    def from_environment(cls):
        return cls(
            os.getenv("KB_DSN", "postgresql://kb_app:kb_local_app@127.0.0.1:55432/evidence_kb"),
            os.getenv("KB_SEARCH_URL", "http://127.0.0.1:59200"),
            Path(os.getenv("KB_STORAGE_ROOT", ".local/store")).resolve(),
            Path(os.getenv("KB_IDENTITIES_FILE", ".local/identities.json")).resolve(),
            os.getenv('KB_SEARCH_BACKEND', 'postgres'),
        )

    def identities(self):
        if not self.identities_file.is_file():
            raise RuntimeError("Run uv run python scripts/bootstrap.py to create local identities.")
        entries = json.loads(self.identities_file.read_text(encoding="utf-8"))
        if not entries or any(len(token) < 32 for token in entries):
            raise RuntimeError("Identity tokens must have at least 32 characters.")
        return {token: Actor(**value) for token, value in entries.items()}
