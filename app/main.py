import asyncio
import contextlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from . import names
from . import __version__, ha_llm, names
from .config import Settings
from .db import DB
from .ha import HA, HAError
from .collector import Collector
from .changes import Changes
from .service import Service
from .api import router
from .mcp_server import build_mcp
from .metrics import render
from .ui import ui_router
from pathlib import Path

INGRESS_PROXY = '172.30.32.2'  # the Supervisor's ingress proxy address on the hassio network


def create_app(settings=None, ha=None, start_collector=True):
    settings = settings or Settings()
    # The factory stays importable for tooling; startup refuses missing credentials.
    @asynccontextmanager
    async def lifespan(app):
        if not settings.token:
            raise RuntimeError('AR_TUNER_TOKEN is required; refusing to start')
        logging.basicConfig(format='%(asctime)s %(levelname)s %(name)s: %(message)s')
        logging.getLogger(names.LOGGER).setLevel(os.getenv('LOG_LEVEL', 'INFO').upper())
        db = DB(settings.db_path)
        client = ha or HA(settings)
        changes = Changes(db, client, settings)
        service = Service(db, client, changes)
        async def adopt_sentences():
            if changes.sentences.writer.configuration()['configured']:
                try:
                    async with changes.lock:
                        await changes.sentences.adopt()
                except (ValueError, HAError) as exc:
                    db.meta('sentence_writer_state', str(exc))
        adoption = asyncio.create_task(adopt_sentences())
        async def follow_ha_llm():
            # AI source "Home Assistant": keep the server's URL and key in step with HA (changed or removed there).
            while True:
                if settings.ai_source == 'ha':
                    await ha_llm.resolve(settings, changes.sentences.writer)
                await asyncio.sleep(ha_llm.CACHE_S)
        following = asyncio.create_task(follow_ha_llm())
        service.diagnose.start()
        app.state.service = service
        app.state.settings = settings
        collector = Collector(db, client, settings)
        app.state.collector = collector
        app.include_router(router(service))
        app.include_router(ui_router(service, settings))
        mcp = build_mcp(service)
        mcp_app = mcp.streamable_http_app()
        app.mount('/mcp', mcp_app)
        app.state.mcp = mcp
        task = asyncio.create_task(collector.run()) if start_collector else None
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for background in (adoption, following):
                background.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await background
            await service.diagnose.close()
            await client.close()
            db.conn.close()

    app = FastAPI(title=names.TITLE, version=__version__, lifespan=lifespan)
    app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')

    @app.middleware('http')
    async def authenticate(request: Request, call_next):
        path = request.url.path
        public = path in {'/healthz', '/metrics', '/login'} or path.startswith('/static/')
        # Home Assistant ingress: the Supervisor proxy (fixed address) has already authenticated an HA user.
        ingress = settings.supervised and request.client is not None and request.client.host == INGRESS_PROXY \
            and request.headers.get('x-ingress-path', '').startswith('/api/hassio_ingress/')
        request.state.root = request.headers['x-ingress-path'].rstrip('/') if ingress else ''
        if not public and not (ingress and not path.startswith(('/api/', '/mcp'))):
            bearer = request.headers.get('authorization', '')
            supplied = bearer[7:] if bearer.startswith('Bearer ') else ''
            api = path.startswith('/api/') or path.startswith('/mcp')
            # Cookies authorize browser pages only; API/MCP require a bearer token.
            if not api and not supplied:
                supplied = request.cookies.get(names.COOKIE, '')
            if not settings.token or not hmac.compare_digest(supplied, settings.token):
                return JSONResponse({'detail': 'Unauthorized'}, status_code=401) if api else RedirectResponse('/login', status_code=303)
        response = await call_next(request)
        location = response.headers.get('location', '')
        if request.state.root and location.startswith('/') and not location.startswith('//'):
            response.headers['location'] = request.state.root + location
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        # Home Assistant shows an ingress App in an iframe on its own origin (the same origin as the ingress URL), so
        # framing is allowed there; everywhere else (the direct port, Docker Compose) it stays forbidden.
        frames = "'self'" if request.state.root else "'none'"
        response.headers['Content-Security-Policy'] = \
            f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors {frames}; form-action 'self'"
        return response

    pages = Jinja2Templates(directory=Path(__file__).parent / 'templates')

    def failure(request, detail, status_code):
        # Browser pages (the wizard) get a readable page with a way back; API, MCP and fetch() callers keep JSON.
        path = request.url.path
        if path.startswith(('/api/', '/mcp', '/ui/')) or 'text/html' not in request.headers.get('accept', ''):
            return JSONResponse({'detail': detail}, status_code=status_code)
        root = getattr(request.state, 'root', '')
        back = request.headers.get('referer', '')
        return pages.TemplateResponse(request=request, name='error.html', status_code=status_code,
                                      context=dict(detail=detail, root=root, back=back if back.startswith(str(request.base_url)) or (root and root in back) else root + '/'))

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return failure(request, str(exc), 400)

    @app.exception_handler(ValidationError)
    async def invalid_model(request, exc):
        return failure(request, 'Invalid change: ' + str(exc), 422)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return failure(request, str(exc).strip("'"), 404)

    @app.exception_handler(HAError)
    async def unavailable(request, exc):
        return failure(request, 'Home Assistant unavailable', 503)

    @app.get('/healthz')
    async def health():
        return {'status': 'ok'}

    @app.get('/metrics')
    async def metrics():
        return Response(render(app.state.service.db), media_type='text/plain; version=0.0.4')

    return app


app = create_app()
