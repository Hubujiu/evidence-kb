from .contract_transfer import (
    CHUNK_BYTES as CHUNK_BYTES, MAX_MESSAGE_BYTES as MAX_MESSAGE_BYTES,
    MAX_VALUE_BYTES as MAX_VALUE_BYTES, JsonClient, JsonEndpoint, TransferError,
    encoded as encode_json, wire as json_value,
)
from .database_contracts import DatabaseError


def _storage(operation, *args, **options):
    try:
        return operation(*args, **options)
    except TransferError as error:
        raise DatabaseError(error.code.replace('CONTRACT_WIRE_', 'STORAGE_WIRE_', 1)) from error


def encoded(value, *, maximum=MAX_MESSAGE_BYTES):
    return _storage(encode_json, value, maximum=maximum)


def wire(value, *, maximum=MAX_MESSAGE_BYTES):
    return _storage(json_value, value, maximum=maximum)


class StorageJsonEndpoint(JsonEndpoint):
    def execute(self, request):
        return _storage(super().execute, request)

    def result(self, value, *, transaction=None):
        return _storage(super().result, value, transaction=transaction)


class StorageJsonClient(JsonClient):
    def call(self, request):
        return _storage(super().call, request)
