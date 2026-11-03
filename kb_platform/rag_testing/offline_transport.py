from contextlib import contextmanager
from unittest.mock import patch

import httpx


_REAL_CLIENT = httpx.Client


def reject_uncaptured_send(request):
    raise AssertionError('Offline fixture sent before its capture transport was installed')


@contextmanager
def offline_client_construction():
    clients = []

    def construct(*args, **kwargs):
        if any(name in kwargs for name in ('transport', 'proxy', 'mounts')):
            raise ValueError('Offline construction cannot replace an explicit transport or proxy')
        client = _REAL_CLIENT(*args, **kwargs, transport=httpx.MockTransport(reject_uncaptured_send))
        clients.append(client)
        return client

    try:
        with patch('kb_platform.remote.httpx.Client', new=construct):
            yield
    except BaseException:
        for client in clients:
            client.close()
        raise
