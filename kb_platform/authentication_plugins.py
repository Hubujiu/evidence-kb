from .authentication import SessionAuthentication, StaticAuthentication
from .plugin_configuration import config_schema
from .plugin_host import PluginSpec
from .storage_plugins import require_storage
from .storage_sdk import KV_EXPIRING_V1, StorageSlot


SESSIONS = StorageSlot('sessions', KV_EXPIRING_V1)


def authentication_plugins(settings):
    def static(ctx, config):
        ctx.provide('authentication', StaticAuthentication(settings.identities()))

    def sessions(ctx, config):
        ctx.provide('authentication', SessionAuthentication(require_storage(ctx, SESSIONS)))

    return (
        PluginSpec('authentication.file', ('authentication',), (), static, config_schema=config_schema()),
        PluginSpec('authentication.sessions', ('authentication',), (SESSIONS,), sessions,
                   config_schema=config_schema()),
    )
