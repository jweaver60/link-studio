from __future__ import annotations

import html
import json
import secrets
import socket
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def local_address() -> str:
    """Best-effort LAN address selection without sending application data."""

    connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        connection.connect(("192.0.2.1", 9))
        return str(connection.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        connection.close()


class RemoteServer:
    """Token-authenticated, LAN-local phone controller with no cloud dependency."""

    def __init__(
        self,
        state_provider: Callable[[], dict[str, Any]],
        action_handler: Callable[[str, Any], None],
        bind: str = "0.0.0.0",
    ) -> None:
        self.state_provider = state_provider
        self.action_handler = action_handler
        self.bind = bind
        self.token = secrets.token_urlsafe(18)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.server is not None

    @property
    def url(self) -> str | None:
        if not self.server:
            return None
        return f"http://{local_address()}:{self.server.server_port}/?token={self.token}"

    def start(self) -> str:
        if self.server:
            return self.url or ""
        self.token = secrets.token_urlsafe(18)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "LinkStudioRemote/1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _token_valid(self) -> bool:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                supplied = self.headers.get("X-Link-Studio-Token") or query.get("token", [""])[0]
                return secrets.compare_digest(supplied.encode("utf-8"), owner.token.encode("ascii"))

            def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
                encoded = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return
                if not self._token_valid():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid pairing token"})
                    return
                if parsed.path == "/api/state":
                    try:
                        state = owner.state_provider()
                    except Exception as exc:
                        self._send_json(
                            HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"state unavailable: {exc}"}
                        )
                        return
                    self._send_json(HTTPStatus.OK, {"ok": True, "state": state})
                    return
                if parsed.path not in {"", "/"}:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                document = owner._document().encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(document)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(document)

            def do_POST(self) -> None:
                if urlparse(self.path).path != "/api/action":
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if not self._token_valid():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid pairing token"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length <= 0 or length > 16_384:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request size"})
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                    action = payload["action"]
                    if not isinstance(action, str):
                        raise TypeError("action must be a string")
                    owner.action_handler(action, payload.get("value"))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception as exc:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                    return
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True})

        server = ThreadingHTTPServer((self.bind, 0), Handler)
        server.daemon_threads = True
        self.server = server
        self.thread = threading.Thread(
            target=server.serve_forever, name="link-studio-remote", daemon=True
        )
        self.thread.start()
        return self.url or ""

    def stop(self) -> None:
        server, self.server = self.server, None
        thread, self.thread = self.thread, None
        if server:
            server.shutdown()
            server.server_close()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _document(self) -> str:
        token = html.escape(self.token, quote=True)
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Link Studio Remote</title>
<style>
:root{{--bg:#17171c;--card:#24242b;--fg:#f4f4f7;--muted:#a8a8b3;--accent:#89b4fa;--danger:#f38ba8}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif}}
main{{max-width:680px;margin:auto;padding:calc(20px + env(safe-area-inset-top)) 18px 32px}}
h1{{font-size:1.45rem;margin:0}}#device{{color:var(--muted);margin:.25rem 0 1.25rem}}
.card{{background:var(--card);border-radius:18px;padding:14px;margin:12px 0;box-shadow:0 7px 28px #0003}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}}.modes{{grid-template-columns:repeat(2,1fr)}}
button{{min-height:48px;border:0;border-radius:13px;background:#ffffff12;color:var(--fg);font:inherit;font-weight:650;padding:10px}}
button:active,button.active{{background:var(--accent);color:#111}}button.danger{{color:var(--danger)}}
.center{{grid-column:2}}label{{display:flex;justify-content:space-between;color:var(--muted);margin:4px 1px 8px}}
input[type=range]{{width:100%;accent-color:var(--accent)}}.status{{display:flex;justify-content:space-between;gap:12px}}
.dot{{width:10px;height:10px;border-radius:50%;background:#a6e3a1;display:inline-block;margin-right:7px}}
</style></head><body><main>
<h1>Link Studio</h1><p id="device">Connecting…</p>
<section class="card"><div class="status"><span><i class="dot"></i><b id="status">Ready</b></span><span id="format"></span></div></section>
<section class="card"><div class="grid modes">
<button onclick="act('mode','normal')">Normal</button><button onclick="act('mode','tracking')">AI Tracking</button>
<button onclick="act('mode','whiteboard')">Whiteboard</button><button onclick="act('mode','overhead')">Overhead</button>
<button onclick="act('mode','deskview')">DeskView</button><button onclick="act('center')">Center</button>
</div></section>
<section class="card"><div class="grid">
<span></span><button onclick="act('move','up')">▲</button><span></span>
<button onclick="act('move','left')">◀</button><button onclick="act('center')">●</button><button onclick="act('move','right')">▶</button>
<span></span><button onclick="act('move','down')">▼</button><span></span>
</div></section>
<section class="card"><label><span>Zoom</span><b id="zoomValue">100%</b></label>
<input id="zoom" type="range" min="100" max="400" step="5" value="100"></section>
<section class="card"><div class="grid">
<button id="preview" onclick="act('preview')">Preview</button><button id="record" onclick="act('record')">Record</button><button onclick="act('screenshot')">Screenshot</button>
<button id="mirror" onclick="act('mirror')">Mirror</button><button id="hdr" onclick="act('hdr')">HDR</button><button id="privacy" class="danger" onclick="act('privacy')">Privacy</button>
</div></section>
</main><script>
const token='{token}', q='?token='+encodeURIComponent(token); let timer;
async function act(action,value){{await fetch('/api/action'+q,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{action,value}})}});setTimeout(refresh,120)}}
async function refresh(){{try{{const r=await fetch('/api/state'+q,{{cache:'no-store'}}),j=await r.json(),s=j.state;
document.getElementById('device').textContent=s.device||'Insta360 Link';document.getElementById('status').textContent=s.status||'Ready';
document.getElementById('format').textContent=s.format||'';document.getElementById('zoom').value=s.zoom||100;document.getElementById('zoomValue').textContent=(s.zoom||100)+'%';
for(const k of ['preview','record','mirror','hdr','privacy'])document.getElementById(k).classList.toggle('active',!!s[k]);
if(s.theme)for(const [k,v] of Object.entries(s.theme))document.documentElement.style.setProperty('--'+k,v);
}}catch(e){{document.getElementById('status').textContent='Disconnected'}}}}
document.getElementById('zoom').addEventListener('input',e=>{{document.getElementById('zoomValue').textContent=e.target.value+'%';clearTimeout(timer);timer=setTimeout(()=>act('zoom',+e.target.value),90)}});
refresh();setInterval(refresh,1200);
</script></body></html>"""
