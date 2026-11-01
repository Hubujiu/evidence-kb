import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import subprocess
import time
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text

from .runner import write_json


ROOT = Path(__file__).resolve().parents[2]
LABEL = 'evidence-kb.storage-acceptance'
IMAGES = {
    'postgres': 'evidence-kb-postgres:18.6-textsearch1.5.1-vector0.8.6',
    'mysql': 'mysql:8.4',
    'redis': 'redis:8.2.10',
    's3': 'docweave/minio:RELEASE.2025-10-15T17-29-55Z',
}


def docker(*arguments, input=None):
    result = subprocess.run(['docker', *arguments], input=input, capture_output=True,
                            encoding='utf-8', timeout=180, check=False)
    if result.returncode:
        raise RuntimeError('STORAGE_RESOURCE_DOCKER_FAILED:' + arguments[0])
    return result.stdout.strip()


def wait_for(check, label):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('STORAGE_RESOURCE_NOT_READY:' + label)


def sql_ready(url):
    engine = create_engine(url, connect_args={'connect_timeout': 2})
    try:
        with engine.connect() as connection:
            return connection.execute(text('SELECT 1')).scalar() == 1
    finally:
        engine.dispose()


def prepare(output):
    output.mkdir(parents=True, exist_ok=False)
    run_id = uuid4().hex
    password = secrets.token_hex(24)
    database = 'rust_plugin_acceptance_contract_' + run_id[:12]
    dense = database + '_dense'
    document = {'run_id': run_id, 'containers': {}, 'images': {}, 'environment': {},
                'created_at': datetime.now(timezone.utc).isoformat()}
    private = output / 'resources-private.json'
    write_json(private, document)
    try:
        settings = {
            'postgres': ({'POSTGRES_USER': 'kb_admin', 'POSTGRES_PASSWORD': password,
                          'POSTGRES_DB': database}, 5432, []),
            'mysql': ({'MYSQL_ROOT_PASSWORD': password, 'MYSQL_ROOT_HOST': '%',
                       'MYSQL_DATABASE': database}, 3306, []),
            'redis': ({}, 6379, ['redis-server', '--save', '', '--appendonly', 'no']),
            's3': ({'MINIO_ROOT_USER': 'storage_acceptance', 'MINIO_ROOT_PASSWORD': password},
                   9000, ['server', '/data']),
        }
        for kind, (environment, port, command) in settings.items():
            image = IMAGES[kind]
            inspected = json.loads(docker('image', 'inspect', image))[0]
            document['images'][kind] = {'tag': image, 'id': inspected['Id']}
            name = 'kb-storage-contract-' + run_id[:12] + '-' + kind
            env_file = output / (kind + '.env')
            env_file.write_text(''.join(key + '=' + value + '\n' for key, value in environment.items()), 'utf-8')
            docker('run', '-d', '--name', name, '--label', LABEL + '=' + run_id,
                   '--env-file', str(env_file), '--publish', '127.0.0.1::' + str(port),
                   '--memory', '1g', image, *command)
            document['containers'][kind] = name
            write_json(private, document)
            actual = json.loads(docker('inspect', name))[0]['NetworkSettings']['Ports'][str(port) + '/tcp']
            if len(actual) != 1 or actual[0]['HostIp'] != '127.0.0.1':
                raise RuntimeError('STORAGE_RESOURCE_NONLOCAL_BINDING')
            document.setdefault('ports', {})[kind] = int(actual[0]['HostPort'])
            env_file.unlink()
            write_json(private, document)
        ports = document['ports']
        pg = f'postgresql://kb_admin:{password}@127.0.0.1:{ports["postgres"]}/{database}'
        mysql = f'mysql+pymysql://root:{password}@127.0.0.1:{ports["mysql"]}/{database}'
        wait_for(lambda: sql_ready(pg), 'postgres')
        wait_for(lambda: sql_ready(mysql), 'mysql')
        pg_container = document['containers']['postgres']
        docker('exec', pg_container, 'createdb', '-U', 'kb_admin', dense)
        for selected in (database, dense):
            for migration in sorted((ROOT / 'sql').glob('*.sql')):
                docker('exec', '-i', pg_container, 'psql', '-U', 'kb_admin', '-d', selected,
                       '-v', 'ON_ERROR_STOP=1', input=migration.read_text('utf-8'))
        import redis

        redis_url = f'redis://127.0.0.1:{ports["redis"]}/0'
        cache = redis.Redis.from_url(redis_url, socket_timeout=2)
        try:
            wait_for(cache.ping, 'redis')
        finally:
            cache.close()
        s3 = f'http://127.0.0.1:{ports["s3"]}'
        wait_for(lambda: httpx.get(s3 + '/minio/health/live', timeout=2).status_code == 200, 's3')
        document['environment'] = {
            'KB_STORAGE_TEST_POSTGRES_URL': pg,
            'KB_STORAGE_TEST_DENSE_POSTGRES_URL': pg.rsplit('/', 1)[0] + '/' + dense,
            'KB_STORAGE_TEST_MYSQL_URL': mysql,
            'KB_STORAGE_TEST_REDIS_URL': redis_url,
            'KB_STORAGE_TEST_S3_ENDPOINT': s3,
            'KB_STORAGE_TEST_S3_ACCESS_KEY': 'storage_acceptance',
            'KB_STORAGE_TEST_S3_SECRET_KEY': password,
        }
        write_json(private, document)
        write_json(output / 'resources.json', {key: value for key, value in document.items() if key != 'environment'})
        return document
    except BaseException:
        cleanup(private)
        raise


def cleanup(private):
    document = json.loads(private.read_text('utf-8'))
    run_id = document['run_id']
    observations = []
    for kind, name in reversed(tuple(document['containers'].items())):
        if name != 'kb-storage-contract-' + run_id[:12] + '-' + kind:
            raise ValueError('STORAGE_RESOURCE_NAME_MISMATCH')
        result = subprocess.run(['docker', 'inspect', name], capture_output=True, encoding='utf-8',
                                timeout=30, check=False)
        if result.returncode:
            observations.append({'name': name, 'result': 'already absent'})
            continue
        inspected = json.loads(result.stdout)[0]
        if inspected['Config'].get('Labels', {}).get(LABEL) != run_id:
            raise ValueError('STORAGE_RESOURCE_OWNERSHIP_MISMATCH')
        docker('rm', '-f', '-v', name)
        observations.append({'name': name, 'result': 'removed'})
    write_json(private.parent / 'cleanup.json', {'run_id': run_id, 'containers': observations, 'result': 'PASS'})
    private.unlink()
    return observations


def main():
    parser = argparse.ArgumentParser(description='Create or remove exclusively owned loopback storage fixtures.')
    parser.add_argument('operation', choices=('prepare', 'cleanup'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.operation == 'prepare':
        document = prepare(args.output.resolve())
        print(json.dumps({'run_id': document['run_id'], 'ports': document['ports'], 'status': 'READY'}))
    else:
        observations = cleanup(args.output.resolve() / 'resources-private.json')
        print(json.dumps({'removed_or_absent': len(observations), 'status': 'PASS'}))


if __name__ == '__main__':
    main()
