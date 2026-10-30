def echo(request):
    return {'args': request['args'], 'kwargs': request['kwargs'],
            'operation': request['identity']['operation']}
