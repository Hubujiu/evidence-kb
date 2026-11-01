import json
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from kb_platform.storage_catalog import initialize_configuration

    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    identity_path = local / "identities.json"
    if not identity_path.exists():
        identities = {
            secrets.token_urlsafe(36): {"tenant_id": "local-demo", "principal_id": principal}
            for principal in ("alice", "bob")
        }
        identity_path.write_text(json.dumps(identities, indent=2), encoding="utf-8")
    print(f"Local identities ready at {identity_path}. Tokens are not printed into logs.")
    storage_path = local / 'storage.json'
    if not storage_path.exists():
        template = json.loads((ROOT / 'config/storage.local.example.json').read_text('utf-8'))
        for instance in template['instances'].values():
            instance['config']['root'] = str(ROOT / instance['config']['root'])
        initialized = initialize_configuration(template)
        with storage_path.open('x', encoding='utf-8') as stream:
            json.dump(initialized, stream, ensure_ascii=False, indent=2)
    print(f"Storage configuration ready at {storage_path}. Existing datasets are kept.")
    print("Start API with uv run uvicorn kb_platform.api:app --host 127.0.0.1 --port 58080.")


if __name__ == "__main__":
    main()
