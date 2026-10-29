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
