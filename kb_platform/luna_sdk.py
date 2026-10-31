from openai import APIConnectionError, APIStatusError, OpenAI, __version__


TRANSPORT_MANIFEST = {'adapter': 'luna-openai-sdk/1', 'sdk': 'openai', 'version': __version__,
                      'sdk_retries': 0, 'response': 'raw', 'http_client': 'caller_owned'}


class _LunaSDK(OpenAI):
    @property
    def default_headers(self):

        return {"Accept": "application/json", "Content-Type": "application/json", **self.auth_headers}


def chat_response(client, base_url, headers, payload, timeout):


    sdk = _LunaSDK(api_key=headers['Authorization'].removeprefix('Bearer '),
                   base_url=base_url, http_client=client, max_retries=0, timeout=timeout)
    try:
        return sdk.chat.completions.with_raw_response.create(
            **payload, extra_headers=headers, timeout=timeout).http_response
    except APIStatusError as exc:

        return exc.response
    except APIConnectionError as exc:


        if exc.__cause__ is not None:
            raise exc.__cause__ from None
        raise
