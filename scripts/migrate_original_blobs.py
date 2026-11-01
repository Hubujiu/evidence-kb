import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb_platform.blob_migration import migrate_legacy_originals
from kb_platform.blob_service import BlobService
from kb_platform.storage import FileBlobProvider, OriginalStore, S3BlobProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path, required=True,
        help='JSON array containing tenant_id, original_path and original_sha256 for every object.')
    parser.add_argument('--legacy-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New audit directory; must not exist.')
    parser.add_argument('--space', default='platform.originals')
    destinations = parser.add_mutually_exclusive_group(required=True)
    destinations.add_argument('--files-root', type=Path)
    destinations.add_argument('--s3-endpoint')
    parser.add_argument('--s3-bucket')
    parser.add_argument('--s3-prefix', default='originals')
    parser.add_argument('--s3-region', default='us-east-1')
    parser.add_argument('--s3-access-key-env', default='KB_BLOB_ACCESS_KEY')
    parser.add_argument('--s3-secret-key-env', default='KB_BLOB_SECRET_KEY')
    args = parser.parse_args()
    if args.s3_endpoint and (not args.s3_bucket or any(not os.environ.get(key)
            for key in (args.s3_access_key_env, args.s3_secret_key_env))):
        parser.error('S3 requires bucket and nonempty access/secret environment variables.')
    records = json.loads(args.records.read_text(encoding='utf-8'))
    service = BlobService()
    provider = (FileBlobProvider(args.files_root) if args.files_root else
        S3BlobProvider(endpoint_url=args.s3_endpoint, bucket=args.s3_bucket,
            access_key=os.environ[args.s3_access_key_env], secret_key=os.environ[args.s3_secret_key_env],
            prefix=args.s3_prefix, region=args.s3_region))
    service.register('destination', provider)
    try:
        store = OriginalStore(bound=service.bind('destination', space=args.space))
        results = migrate_legacy_originals(records, legacy_root=args.legacy_root, target=store, output=args.output)
        print(json.dumps({'status': 'COMPLETE', 'objects': len(results), 'database_updated': False}))
    finally:
        service.close()


if __name__ == '__main__':
    main()
