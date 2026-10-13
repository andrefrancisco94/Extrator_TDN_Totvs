"""Servidor Confluence/TDN falso + proxy HTTP local (somente loopback).

Helper de teste: permite validar o extrator de ponta a ponta (crawl, export,
rate limit, retry, rotacao de proxy) sem fazer nenhum request ao tdn.totvs.com.

FakeTDN simulates:
  * a page tree (display URLs and viewpage.action?pageId= URLs)
  * GET /rest/api/content/{id}/child/page?limit=&start=&expand=children.page
  * cacheable static assets, per-page images, junk XHRs (analytics/quickreload)
  * a Cloudflare-like rate limit per client IP: more than `limit` counted
    requests inside `window` seconds => HTTP 429 + Retry-After + error-1015
    body for `ban_seconds`. Requests arriving during a ban are recorded as
    "pokes". The ones arriving more than `poke_grace` seconds after the ban
    started are "late pokes": the browser fires a page's sub-requests in
    parallel, so a few land right after the request that triggered the ban,
    but once the extractor has seen the 429 it must stay quiet until the ban
    is over.

LocalProxy is a tiny threaded HTTP proxy. It connects to the origin from its
own loopback source address (e.g. 127.0.0.2) so the origin sees each proxy as a
different client IP, like a real exit IP.
"""
from __future__ import annotations

import json
import re
import select
import socket
import threading
import time
from collections import Counter, defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlparse

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)

BLOCK_BODY = (
    "<!DOCTYPE html><html><head><title>Access denied | fake used Cloudflare to restrict access"
    "</title></head><body><div class=\"cf-error-details\"><h1>Error 1015</h1>"
    "<span>Ray ID: fake123</span><h2>You are being rate limited</h2>"
    "<p>The owner of this website has banned you temporarily from accessing this website.</p>"
    "</div></body></html>"
).encode()


class FakeTDN:
    """tree: {page_id: (title, [child_ids], style)}; style in {'display', 'viewpage'}."""

    def __init__(self, tree, root_id, *, limit=10_000, window=10.0, ban_seconds=3.0,
                 images_per_page=2, expand_supported=True, api_enabled=True,
                 slow_ids=(), fail_once_ids=(), login_ids=(), image_429_once_ids=(),
                 poke_grace=1.0):
        self.tree = tree
        self.root_id = root_id
        self.limit = limit
        self.window = window
        self.ban_seconds = ban_seconds
        self.images_per_page = images_per_page
        self.expand_supported = expand_supported
        self.api_enabled = api_enabled
        self.fail_once_ids = set(fail_once_ids)
        self.login_ids = set(login_ids)
        # Paginas cujo HTML responde 200 mas as IMAGENS respondem 429 no
        # primeiro carregamento (rate limit estourando no meio da pagina).
        self.image_429_once_ids = set(image_429_once_ids)
        self.image_429_served: Counter = Counter()
        self.slow_ids = set(slow_ids)
        self.lock = threading.Lock()
        self.hits: Counter = Counter()          # category -> count
        self.hits_by_ip: Counter = Counter()
        self.page_hits: Counter = Counter()     # page id -> html hits
        self.api_hits: Counter = Counter()      # page id -> child api hits
        self.pokes: Counter = Counter()         # ip -> requests received while banned
        self.late_pokes: Counter = Counter()    # ip -> pokes after the grace period
        self.poke_grace = poke_grace
        self._ban_started: dict[str, float] = {}
        self.bans: list[tuple[str, float]] = []
        self._recent: dict[str, deque] = defaultdict(deque)
        self._banned_until: dict[str, float] = {}
        self.by_title = {t: pid for pid, (t, _c, _s) in tree.items()}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status, body, ctype, extra=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self._dispatch()

            def do_GET(self):
                self._dispatch()

            def _dispatch(self):
                try:
                    outer._handle(self)
                except (ConnectionError, OSError):
                    pass

        class QuietServer(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # reset de conexao pelo browser e ruido esperado

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    # ------------------------------------------------------------------ urls
    def url_for(self, pid, public=True):
        title, _children, style = self.tree[pid]
        if style == "viewpage":
            return f"{self.base}/pages/viewpage.action?pageId={pid}"
        prefix = "/display/public/PROT/" if public else "/display/PROT/"
        return f"{self.base}{prefix}{quote_plus(title)}"

    def webui(self, pid):
        title, _children, style = self.tree[pid]
        if style == "viewpage":
            return f"/pages/viewpage.action?pageId={pid}"
        return f"/display/PROT/{quote_plus(title)}"

    def bfs_order(self):
        order, queue = [], deque([self.root_id])
        while queue:
            pid = queue.popleft()
            order.append(pid)
            queue.extend(self.tree[pid][1])
        return order

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------- rate limit
    def _rate_limited(self, ip, counted):
        """Returns remaining ban seconds (>0) if this request must be rejected."""
        now = time.monotonic()
        with self.lock:
            until = self._banned_until.get(ip, 0.0)
            if until > now:
                self.pokes[ip] += 1
                if now - self._ban_started.get(ip, now) > self.poke_grace:
                    self.late_pokes[ip] += 1
                return until - now
            if not counted:
                return 0.0
            recent = self._recent[ip]
            recent.append(now)
            while recent and recent[0] < now - self.window:
                recent.popleft()
            if len(recent) > self.limit:
                self._banned_until[ip] = now + self.ban_seconds
                self._ban_started[ip] = now
                self.bans.append((ip, now))
                recent.clear()
                return self.ban_seconds
        return 0.0

    # ---------------------------------------------------------------- handler
    def _handle(self, h):
        parsed = urlparse(h.path)
        path, query = parsed.path, parse_qs(parsed.query)
        ip = h.client_address[0]
        if path == "/cdn-cgi/trace":
            # Served by the edge (as on Cloudflare): outside the rate limit.
            h._send(200, f"fl=0\nh=fake\nip={ip}\nts=0\n".encode(), "text/plain")
            return
        static = path.startswith("/s/static/") or path == "/favicon.ico"
        category = self._category(path)
        with self.lock:
            self.hits[category] += 1
            self.hits_by_ip[ip] += 1
        remaining = self._rate_limited(ip, counted=not static)
        if remaining > 0:
            h._send(429, BLOCK_BODY, "text/html; charset=UTF-8",
                    {"Retry-After": str(max(1, int(remaining + 0.999))), "Server": "cloudflare"})
            return

        if static:
            body = b"body{font-family:sans-serif}" if path.endswith(".css") else b"window.fake=1;"
            ctype = "text/css" if path.endswith(".css") else "application/javascript"
            h._send(200, body, ctype, {"Cache-Control": "public, max-age=31536000"})
            return
        if path.startswith("/download/attachments/"):
            owner = path.split("/")[3] if path.count("/") >= 4 else ""
            with self.lock:
                blocked = owner in self.image_429_once_ids
                if blocked:
                    self.image_429_served[owner] += 1
            if blocked:
                h._send(429, BLOCK_BODY, "text/html; charset=UTF-8",
                        {"Retry-After": "1", "Server": "cloudflare"})
                return
            h._send(200, PNG, "image/png", {"Cache-Control": "private"})
            return
        match = re.match(r"^/rest/api/content/(\d+)/child/page$", path)
        if match:
            self._child_api(h, match.group(1), query)
            return
        if path.startswith("/rest/"):
            h._send(200, b"{}", "application/json")
            return
        pid = self._resolve_page(path, query)
        if pid is None:
            h._send(404, b"<html><title>Not found</title></html>", "text/html")
            return
        self._page(h, pid)

    @staticmethod
    def _category(path):
        if path.startswith("/s/static/"):
            return "static"
        if path.startswith("/download/"):
            return "image"
        if path.startswith("/rest/api/content/"):
            return "api"
        if path.startswith("/rest/analytics/") or path.startswith("/rest/quickreload/"):
            return "junk"
        if path.startswith("/rest/"):
            return "rest-other"
        if path.startswith("/display/") or path.startswith("/pages/"):
            return "page"
        return "other"

    def _resolve_page(self, path, query):
        if path == "/pages/viewpage.action":
            pid = (query.get("pageId") or [""])[0]
            return pid if pid in self.tree else None
        match = re.match(r"^/display/(?:public/)?PROT/(.+)$", path)
        if match:
            return self.by_title.get(unquote_plus(match.group(1)))
        return None

    def _child_api(self, h, pid, query):
        with self.lock:
            self.api_hits[pid] += 1
        if not self.api_enabled or pid not in self.tree:
            h._send(404, b'{"statusCode":404,"message":"not found"}', "application/json")
            return
        limit = int((query.get("limit") or ["25"])[0])
        start = int((query.get("start") or ["0"])[0])
        expand = (query.get("expand") or [""])[0]
        children = self.tree[pid][1]
        chunk = children[start:start + limit]
        results = []
        for cid in chunk:
            item = {"id": cid, "type": "page", "title": self.tree[cid][0],
                    "_links": {"webui": self.webui(cid)}}
            if self.expand_supported and "children.page" in expand:
                grand = self.tree[cid][1]
                item["children"] = {"page": {
                    "results": [{"id": g, "title": self.tree[g][0],
                                 "_links": {"webui": self.webui(g)}} for g in grand[:25]],
                    "start": 0, "limit": 25, "size": min(len(grand), 25),
                }}
            results.append(item)
        body = json.dumps({"results": results, "start": start, "limit": limit,
                           "size": len(results)}).encode()
        h._send(200, body, "application/json")

    def _page(self, h, pid):
        with self.lock:
            self.page_hits[pid] += 1
            fail_now = pid in self.fail_once_ids
            if fail_now:
                self.fail_once_ids.discard(pid)
            # No segundo carregamento da pagina as imagens voltam a responder 200.
            if self.page_hits[pid] >= 2:
                self.image_429_once_ids.discard(pid)
        if fail_now:
            h._send(503, b"<html><title>503</title>Service Unavailable</html>", "text/html")
            return
        if pid in self.login_ids:
            body = (b"<html><head><title>Log In - Fake</title></head><body>"
                    b"<form id=\"login-form\" name=\"loginform\"><input name=\"os_username\">"
                    b"<input name=\"os_password\"></form></body></html>")
            h._send(200, body, "text/html; charset=utf-8")
            return
        if pid in self.slow_ids:
            time.sleep(1.5)
        title, children, _style = self.tree[pid]
        imgs = "".join(
            f'<p><img src="/download/attachments/{pid}/img-{i}.png" width="40" height="40"></p>'
            for i in range(self.images_per_page)
        )
        tree_links = "".join(
            f'<li><a href="{self.webui(c)}">{self.tree[c][0]}</a></li>' for c in children
        )
        filler = "<p>" + ("Conteudo de exemplo da documentacao. " * 40) + "</p>"
        html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title} - Fake TDN</title>
<meta name="ajs-page-id" content="{pid}">
<link rel="stylesheet" href="/s/static/batch.css">
<script src="/s/static/batch.js"></script></head>
<body>
<div class="ia-splitter-left"><div class="plugin_pagetree"><ul>
<li><span class="plugin_pagetree_children_span" data-page-id="{pid}"></span>
<a href="{self.webui(pid)}">{title}</a><ul>{tree_links}</ul></li></ul></div></div>
<div id="main-content" class="wiki-content"><h1>{title}</h1>{filler}{imgs}
<div class="expand-container"><div class="expand-content" style="display:none">oculto {pid}</div></div>
</div>
<script>
fetch('/rest/analytics/1.0/publish/bulk', {{method: 'POST', body: '[]'}}).catch(() => 0);
fetch('/rest/quickreload/latest/{pid}?since=0').catch(() => 0);
</script></body></html>"""
        h._send(200, html.encode(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})


class LocalProxy:
    """Threaded HTTP forward proxy (CONNECT + absolute-URI). Loopback only.

    source_ip: loopback address used to reach the origin (acts as the exit IP).
    """

    def __init__(self, source_ip="127.0.0.2", fail=False):
        self.source_ip = source_ip
        self.fail = fail
        self.connections = 0
        self.refused_external = Counter()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._stopped = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self):
        self._stopped = True
        try:
            self.listener.close()
        except OSError:
            pass

    def _accept_loop(self):
        while not self._stopped:
            try:
                conn, _addr = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        upstream = None
        try:
            conn.settimeout(20)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                head += chunk
            header_blob, rest = head.split(b"\r\n\r\n", 1)
            lines = header_blob.split(b"\r\n")
            method, target, _version = lines[0].decode("latin-1").split(" ", 2)
            if method == "CONNECT":
                host, port = target.rsplit(":", 1)
            else:
                parsed = urlparse(target)
                host, port = parsed.hostname or "", parsed.port or 80
            # Loopback only: never forward the browser's own background traffic.
            if self.fail or not str(host).startswith("127."):
                self.refused_external[str(host)] += 1
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.bind((self.source_ip, 0))
            upstream.settimeout(20)
            upstream.connect((host, int(port)))
            self.connections += 1
            if method == "CONNECT":
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            else:
                origin_form = parsed.path or "/"
                if parsed.query:
                    origin_form += "?" + parsed.query
                new_lines = [f"{method} {origin_form} HTTP/1.1".encode("latin-1")]
                for line in lines[1:]:
                    lower = line.lower()
                    if lower.startswith(b"proxy-connection:") or lower.startswith(b"connection:"):
                        continue
                    new_lines.append(line)
                new_lines.append(b"Connection: close")
                upstream.sendall(b"\r\n".join(new_lines) + b"\r\n\r\n" + rest)
            self._relay(conn, upstream)
        except (OSError, ValueError):
            pass
        finally:
            for sock in (conn, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

    @staticmethod
    def _relay(a, b):
        socks = [a, b]
        while True:
            readable, _w, errored = select.select(socks, [], socks, 30)
            if errored or not readable:
                return
            for sock in readable:
                try:
                    data = sock.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                (b if sock is a else a).sendall(data)


def sample_tree(leaves_per_branch=4, branches=3, deep=True):
    """Root -> `branches` sections -> leaves; optional third level under section 1."""
    tree = {}
    next_id = [100]

    def new(title, style):
        next_id[0] += 1
        pid = str(next_id[0])
        tree[pid] = (title, [], style)
        return pid

    root = new("CFGTRIB - Raiz", "display")
    for b in range(1, branches + 1):
        section = new(f"Secao {b}", "display" if b % 2 else "viewpage")
        tree[root][1].append(section)
        for leaf in range(1, leaves_per_branch + 1):
            style = "viewpage" if (b + leaf) % 2 else "display"
            child = new(f"Secao {b} - Pagina {leaf}", style)
            tree[section][1].append(child)
            if deep and b == 1 and leaf == 1:
                for g in range(1, 3):
                    tree[child][1].append(new(f"Secao 1 - Pagina 1 - Detalhe {g}", "viewpage"))
    return tree, root

