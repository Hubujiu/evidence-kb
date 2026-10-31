import asyncio
from pathlib import Path
from threading import Event

import uvicorn


class HttpApplication:
    def __init__(self, app, config):
        self.app = app
        self.server = uvicorn.Server(uvicorn.Config(app,
            host=config.get('host', '127.0.0.1'), port=config.get('port', 58080),
            lifespan='off', log_level=config.get('log_level', 'info')))
        self._entered, self._finished = False, False

    async def _serve(self):
        try:
            async with self.app.router.lifespan_context(self.app):
                self._entered = True
                await self.server.serve()
        finally:
            self._finished = self._entered
        if not self.server.started:
            raise RuntimeError('HOST_HTTP_START_FAILED')

    def run(self):
        asyncio.run(self._serve())
        return {'status': 'stopped', 'port': self.server.config.port}

    def stop(self):
        self.server.should_exit = True

    def close(self):
        if not self._finished:
            asyncio.run(self.app.router.shutdown())
            self._finished = True


def _config(config, allowed):
    if set(config) - allowed:
        raise ValueError('UNKNOWN_HOST_APPLICATION_OPTION')
    if 'port' in config and (type(config['port']) is not int or not 0 <= config['port'] <= 65535):
        raise ValueError('INVALID_HOST_APPLICATION_PORT')
    if 'host' in config and (not isinstance(config['host'], str) or not config['host']):
        raise ValueError('INVALID_HOST_APPLICATION_ADDRESS')
    return config


def api(config):
    from .api import create_app

    selected = _config(config, {'profile', 'host', 'port', 'log_level'})
    app = create_app(profile=selected.get('profile'))
    try:
        return HttpApplication(app, selected)
    except BaseException:
        asyncio.run(app.router.shutdown())
        raise


def delivery(config):
    from .delivery import create_delivery_app

    selected = _config(config, {'config', 'providers', 'run_root', 'budget_batch', 'host', 'port', 'log_level'})
    app = create_delivery_app(selected.get('config'), selected.get('providers'),
                              selected.get('run_root'), budget_batch=selected.get('budget_batch'))
    try:
        return HttpApplication(app, selected)
    except BaseException:
        asyncio.run(app.router.shutdown())
        raise


class AgentApplication:
    def __init__(self, config):
        from .harness_profile import create_agent_application
        from .session_identity import Actor

        selected = _config(config, {'profile', 'message', 'tenant', 'actor', 'scope', 'session',
                                   'request_id', 'expected_revision'})
        if not isinstance(selected.get('message'), str) or not selected['message'].strip():
            raise ValueError('AGENT_MESSAGE_REQUIRED')
        profile = selected.get('profile') or Path(__file__).resolve().parents[1] / 'config/agent-profiles/core-models.json'
        self.application, self.host = create_agent_application(profile=profile)
        self.actor = Actor(selected.get('tenant', 'local'), selected.get('actor', 'local'))
        self.config = selected
        self.cancellation = Event()

    def run(self):
        config = self.config
        session = config.get('session') or self.application.create(
            self.actor, config.get('scope', 'default'))['session_id']
        result = self.application.turn(self.actor, session, message=config['message'],
            request_id=config.get('request_id', 'cli-turn'), expected_revision=config.get('expected_revision', 0),
            cancellation=self.cancellation)
        return {'session_id': session, 'response': result}

    def stop(self):
        self.cancellation.set()

    def close(self):
        self.host.close()


def agent(config):
    return AgentApplication(config)
