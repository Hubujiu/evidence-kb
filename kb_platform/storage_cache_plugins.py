from .plugin_configuration import config_schema
from .plugin_host import PluginSpec
from .rag_testing.cache import StageCache
from .storage_plugins import require_storage
from .storage_sdk import KV_EXPIRING_V1, StorageSlot
from .storage_sdk.relational import STRUCTURED_RELATIONAL_V1


INDEX = StorageSlot('index', STRUCTURED_RELATIONAL_V1)
PAYLOAD_CACHE = StorageSlot('payload', KV_EXPIRING_V1)


def stage_cache_plugins():
    def mount(ctx, config):
        cache = StageCache(database=require_storage(ctx, INDEX),
                           payload_cache=require_storage(ctx, PAYLOAD_CACHE),
                           payload_ttl_seconds=config.get('payload_ttl_seconds', 300))
        ctx.defer(cache.close)
        ctx.provide('stage.cache', cache)

    return (PluginSpec('storage.cache.volatile', ('stage.cache',), (), mount,
                       storage_slots=(INDEX, PAYLOAD_CACHE),
                       config_schema=config_schema({'payload_ttl_seconds': {
                           'type': 'number', 'exclusiveMinimum': 0, 'maximum': 365 * 24 * 60 * 60}})),)
