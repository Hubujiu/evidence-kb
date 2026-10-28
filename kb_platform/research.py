"""Shared configured providers and read access to historical source-title mappings.

The Agent uses canonical child retrieval. The former parallel document-level
research retrieval, index builders and generated channels have been removed.
"""
from hashlib import sha256
from pathlib import Path


class ResearchService:
    def __init__(self, platform, luna, embedding, root='.local/research-v4e-pruned'):
        self.platform = platform
        self.luna = luna
        self.embedding = embedding
        self.root = Path(root).resolve()

    def _directory(self, actor, scope_id):
        key = sha256(f'{actor.tenant_id}\0{scope_id}'.encode()).hexdigest()
        return self.root / key
