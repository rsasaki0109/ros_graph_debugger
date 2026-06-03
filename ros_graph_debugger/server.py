"""FastAPI app: REST + WebSocket + AI Markdown, plus the static web UI.

The app is intentionally a thin read layer over RuntimeGraphStore. The rclpy
node fills the store from another thread; here we just serialize and stream it.
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles

from .markdown import snapshot_to_markdown
from .model import RuntimeGraphStore


def create_app(store: RuntimeGraphStore, web_dir: str,
               profile_data: dict | None = None,
               stream_period: float = 1.0) -> FastAPI:
    app = FastAPI(title='ros_graph_debugger', version='0.1.0')

    # Expose only the UI-relevant parts of the profile (groups), not the
    # derived expectation maps used internally.
    profile_public = None
    if profile_data:
        profile_public = {
            'name': profile_data.get('name'),
            'groups': profile_data.get('groups', {}),
        }

    # ------------------------------------------------------------- REST API #
    @app.get('/api/v1/health')
    def health():
        return {'status': 'ok', 'version': '0.1.0'}

    @app.get('/api/v1/profile')
    def profile():
        return profile_public or {}

    @app.get('/api/v1/snapshot')
    def snapshot():
        return JSONResponse(store.snapshot().to_dict())

    @app.get('/api/v1/snapshot.md', response_class=PlainTextResponse)
    def snapshot_md():
        return snapshot_to_markdown(store.snapshot())

    @app.get('/api/v1/graph')
    def graph():
        s = store.snapshot().to_dict()
        return {'timestamp': s['timestamp'], 'profile': s['profile'],
                'nodes': s['nodes'], 'topics': s['topics'], 'edges': s['edges']}

    @app.get('/api/v1/nodes')
    def nodes():
        return store.snapshot().to_dict()['nodes']

    @app.get('/api/v1/topics')
    def topics():
        return store.snapshot().to_dict()['topics']

    @app.get('/api/v1/tf')
    def tf():
        return store.snapshot().to_dict()['tf_edges']

    @app.get('/api/v1/diagnostics')
    def diagnostics():
        return store.snapshot().to_dict()['diagnostics']

    @app.get('/api/v1/issues')
    def issues():
        return store.snapshot().to_dict()['issues']

    # ------------------------------------------------------------ WebSocket #
    @app.websocket('/api/v1/stream')
    async def stream(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                payload = json.dumps(store.snapshot().to_dict())
                await ws.send_text(payload)
                await asyncio.sleep(stream_period)
        except WebSocketDisconnect:
            return
        except Exception:
            return

    # --------------------------------------------------------------- web UI #
    if os.path.isdir(web_dir):
        @app.get('/', response_class=HTMLResponse)
        def index():
            return FileResponse(os.path.join(web_dir, 'index.html'))

        app.mount('/', StaticFiles(directory=web_dir, html=True), name='web')
    else:
        @app.get('/', response_class=HTMLResponse)
        def index_missing():
            return HTMLResponse(
                '<h1>ros_graph_debugger</h1><p>Web assets not found at '
                f'<code>{web_dir}</code>. The REST API is available under '
                '<code>/api/v1/</code>.</p>')

    return app
