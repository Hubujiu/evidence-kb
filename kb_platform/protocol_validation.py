import inspect


def _signature(callback):
    if not callable(callback):
        return None
    target = callback if inspect.isroutine(callback) else getattr(callback, '__call__', None)
    if any(inspect.iscoroutinefunction(value) or inspect.isasyncgenfunction(value)
           or inspect.isgeneratorfunction(value) for value in (callback, target)):
        return None
    try:
        return inspect.signature(callback)
    except (TypeError, ValueError):
        return None


def valid_callable(callback, *args, **kwargs):
    signature = _signature(callback)
    if signature is None:
        return False
    try:
        signature.bind(*args, **kwargs)
    except TypeError:
        return False
    return True


def callable_accepts(callback, protocol_method):
    signature = _signature(callback)
    if signature is None or getattr(callback, '__func__', callback) is protocol_method:
        return False
    try:
        parameters = tuple(inspect.signature(protocol_method).parameters.values())[1:]
    except (TypeError, ValueError):
        return False
    if any(parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD) for parameter in parameters):
        return False
    for include_optional in (False, True):
        selected = [parameter for parameter in parameters
                    if include_optional or parameter.default is parameter.empty]
        positional = [parameter for parameter in selected
                      if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)]
        for split in range(len(positional) + 1):
            args, kwargs = [], {}
            for index, parameter in enumerate(positional):
                if index < split or parameter.kind is parameter.POSITIONAL_ONLY:
                    args.append(None)
                else:
                    kwargs[parameter.name] = None
            kwargs.update((parameter.name, None) for parameter in selected
                          if parameter.kind is parameter.KEYWORD_ONLY)
            try:
                signature.bind(*args, **kwargs)
            except TypeError:
                return False
    return True
