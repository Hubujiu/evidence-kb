"""One SDK attempt over the caller-owned, metered HTTP client."""
from openai import APIConnectionError, APIStatusError, OpenAI, __version__


TRANSPORT_MANIFEST = {'adapter': 'luna-openai-sdk/1', 'sdk': 'openai', 'version': __version__,
                      'sdk_retries': 0, 'response': 'raw', 'http_client': 'caller_owned'}


class _LunaSDK(OpenAI):
    @property
    def default_headers(self):
        # Provider configuration owns routing/auth; ambient OPENAI_* headers do not.
        return {"Accept": "application/json", "Content-Type": "application/json", **self.auth_headers}


def chat_response(client, base_url, headers, payload, timeout):
    # Do not close the SDK: its injected HTTP client belongs to the provider and
    # may have been replaced by observe_client or wrapped by delivery serialization.
    sdk = _LunaSDK(api_key=headers['Authorization'].removeprefix('Bearer '),
                   base_url=base_url, http_client=client, max_retries=0, timeout=timeout)
    try:
        return sdk.chat.completions.with_raw_response.create(
            **payload, extra_headers=headers, timeout=timeout).http_response
    except APIStatusError as exc:
        # The outer runtime owns HTTP classification, Retry-After and retry lineage.
        return exc.response
    except APIConnectionError as exc:
        # SDK wrapping must not turn admission rejection or a read timeout into
        # a retryable connection failure, nor hide a known-zero-send rejection.
        if exc.__cause__ is not None:
            raise exc.__cause__ from None
        raise
