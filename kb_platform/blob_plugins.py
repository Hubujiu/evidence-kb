import os

from .plugin_configuration import TEXT, POSITIVE, config_schema, configured_plugins, environment_inputs
from .plugin_host import PluginSpec


def blob_plugins():
    def registry(ctx, config):
        from .blob_service import BlobService

        service = BlobService()
        ctx.defer(service.close)
        ctx.provide('blobs.registry', service)

    def provider(kind):
        def mount(ctx, config):
            from .storage import FileBlobProvider, S3BlobProvider

            if kind == 'files':
                value = FileBlobProvider(config['root'])
            else:
                value = S3BlobProvider(endpoint_url=config['endpoint_url'], bucket=config['bucket'],
                    access_key=os.environ[config['access_key_env']], secret_key=os.environ[config['secret_key_env']],
                    region=config.get('region', 'us-east-1'), prefix=config.get('prefix', 'originals'),
                    max_bytes=config.get('max_bytes', 64 * 1024 * 1024))
            identity = ':'.join(str(ctx.identity[key]) for key in ('runtime', 'host', 'instance', 'generation'))
            identity += ':' + str(ctx.registration_id(kind='blob_route'))
            route = config.get('route', kind)
            try:
                dispose = ctx.require('blobs.registry').register(route, value,
                    registration_id=identity, owner_lease=ctx.lease)
            except BaseException:
                value.close()
                raise
            ctx.defer(dispose)
            ctx.provide('blobs.route.' + kind, {'route': route, 'registration_id': identity})
        return mount

    def binding(kind):
        def mount(ctx, config):
            marker = ctx.require('blobs.route.' + kind)
            value = ctx.require('blobs.registry').bind(marker['route'], space=config['space'],
                                                       expected=marker['registration_id'])
            ctx.provide('blobs.bound', value)
        return mount

    def check_s3(config):
        if any(not os.environ.get(config[key]) for key in ('access_key_env', 'secret_key_env')):
            raise ValueError('BLOB_CREDENTIALS_REQUIRED')

    def s3_inputs(config):
        check_s3(config)
        return environment_inputs((config['access_key_env'], config['secret_key_env']))

    schemas = {
        'blobs.provider.files': config_schema({'root': TEXT, 'route': TEXT}, required=('root',)),
        'blobs.provider.s3': config_schema({'route': TEXT, 'endpoint_url': TEXT, 'bucket': TEXT,
            'access_key_env': TEXT, 'secret_key_env': TEXT, 'region': TEXT, 'prefix': TEXT, 'max_bytes': POSITIVE},
            required=('endpoint_url', 'bucket', 'access_key_env', 'secret_key_env')),
        **{'blobs.binding.' + kind: config_schema({'space': TEXT}, required=('space',))
           for kind in ('files', 's3')},
    }
    return configured_plugins((PluginSpec('blobs.service', ('blobs.registry',), (), registry),
        *(PluginSpec('blobs.provider.' + kind, ('blobs.route.' + kind,), ('blobs.registry',), provider(kind))
          for kind in ('files', 's3')),
        *(PluginSpec('blobs.binding.' + kind, ('blobs.bound',), ('blobs.registry', 'blobs.route.' + kind), binding(kind))
          for kind in ('files', 's3'))), schemas,
        {'blobs.provider.s3': check_s3}, {'blobs.provider.s3': s3_inputs})
