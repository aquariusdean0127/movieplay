#!/usr/bin/env python3
"""
Combined MovieBox proxy + provider aggregator + HTML server.

Key fixes:
  1. Forces HTTP/2 for upstream API requests — MovieBox gates higher-quality
     video streams (720p/1080p/4K) behind HTTP/2.
  2. Multi-strategy retry for CDN media requests. bcdnxw.hakunaymatata.com
     returns HTTP 426 (Upgrade Required) when it thinks the caller is a bot.
     We try 7 fingerprint / header / HTTP-version combos before giving up.
  3. `direct=1` query param — returns a 302 to the CDN so the user's browser
     fetches it directly (real IP + real TLS = usually not blocked).
  4. /favicon.ico returns 204 instead of 400.
"""

import importlib
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, urljoin, quote

from curl_cffi import requests as creq

# ---------------------------------------------------------------- keepalive
KEEPALIVE_INTERVAL = 4 * 60
KEEPALIVE_TARGETS = [
    "https://api.github.com/zen",
    "https://officialmoviebox.com/",
]

def _keepalive_loop():
    i = 0
    while True:
        target = KEEPALIVE_TARGETS[i % len(KEEPALIVE_TARGETS)]
        i += 1
        try:
            r = creq.get(target, impersonate="chrome120", verify=False,
                         timeout=20, allow_redirects=True, http_version="v2")
            print(f"[keepalive] {time.strftime('%H:%M:%S')} {target} -> {r.status_code}", flush=True)
        except Exception as e:
            print(f"[keepalive] {target} failed: {e}", flush=True)
        time.sleep(KEEPALIVE_INTERVAL)

def start_keepalive():
    threading.Thread(target=_keepalive_loop, name="keepalive", daemon=True).start()

def _on_sigterm(signum, frame):
    print(f"[shutdown] SIGTERM received at {time.strftime('%H:%M:%S')}", flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, _on_sigterm)

# ---------------------------------------------------------------- constants
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HTML = "index.html"

ALLOWED_HOSTS = (
    "officialmoviebox.com", ".officialmoviebox.com",
    ".moviebox.co", ".moviebox.ng", ".moviebox.ph", ".movieboxapi",
    ".aoneroom.com", ".aoneroom", ".aonyx",
    ".bcdnxw.com", ".bcdnw.com", ".pbcdnw", ".macdn",
    "hakunaymatata.com", ".hakunaymatata.com",
    ".hdhub4u.cl", ".hdhub4u.ec", ".hdhub4u.kg", ".hdhub4u.ms",
    "hubcloud.foo", ".hubcloud.foo",
    "hubcloud.ist", ".hubcloud.ist",
    "hubcloud.cx", ".hubcloud.cx",
    "gadgetsweb.xyz", ".gadgetsweb.xyz",
    "hubcdn.fans", ".hubcdn.fans",
    "hubdrive.space", ".hubdrive.space",
    "hubdrive.pics", ".hubdrive.pics",
    "hubstream.xyz", ".hubstream.xyz",
    "pixeldrain.com", ".pixeldrain.com",
    "workers.dev", ".workers.dev",
    ".moviesdrive.beer", ".moviesdrive.net",
    "drive.google.com", "drive.usercontent.google.com",
    "docs.google.com", "gdrive.com",
)

CHUNK_SIZE = 262144

CONTENT_TYPE_OVERRIDES = {
    ".mp4":  "video/mp4",
    ".m4s":  "video/iso.segment",
    ".m4v":  "video/mp4",
    ".ts":   "video/mp2t",
    ".webm": "video/webm",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mpd":  "application/dash+xml",
    ".srt":  "application/x-subrip",
    ".vtt":  "text/vtt",
    ".mkv":  "video/x-matroska",
}

def _guess_content_type_from_path(path):
    p = urlparse(path).path.lower()
    for ext, ctype in CONTENT_TYPE_OVERRIDES.items():
        if p.endswith(ext):
            return ctype
    return None

# ---------------------------------------------------------------- CDN retry helpers
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

def _build_cdn_headers(referer, cookie=None, range_hdr=None):
    h = {
        "User-Agent": _BROWSER_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity;q=1, *;q=0",
        "Referer": referer,
        "Origin": "https://officialmoviebox.com",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Priority": "u=1, i",
        "Upgrade-Insecure-Requests": "1",
    }
    if cookie:    h["Cookie"] = cookie
    if range_hdr: h["Range"]  = range_hdr
    return h

def _is_good_media_response(r):
    if r is None: return False
    if r.status_code >= 400: return False
    ct = (r.headers.get("Content-Type") or "").lower()
    return (
        ct.startswith("video/")
        or ct.startswith("audio/")
        or ct.startswith("application/octet-stream")
        or ct.startswith("application/vnd.apple.mpegurl")
        or ct.startswith("application/x-mpegurl")
        or ct.startswith("application/dash+xml")
    )

def _fetch_media_with_retry(target, referer, cookie, range_hdr):
    """
    Try multiple fingerprint/header/HTTP-version combos until we get real video.
    Returns (response, strategy_label) on success, (None, error_msg) on failure.
    """
    strategies = [
        ("chrome131", "v2",  {}, "chrome131/v2"),
        ("chrome120", "v2",  {}, "chrome120/v2"),
        ("chrome131", "v1.1", {}, "chrome131/v1.1"),
        ("chrome124", "v1.1", {}, "chrome124/v1.1"),
        ("chrome120", "v2",  ({"Range": "bytes=0-"} if not range_hdr else {}),
                              "chrome120/v2+range"),
        ("safari17_0", "v2", {}, "safari17/v2"),
        ("firefox133", "v2", {}, "firefox133/v2"),
    ]

    last_error = None
    for imp, hv, extra, label in strategies:
        try:
            hdrs = _build_cdn_headers(referer, cookie, range_hdr)
            hdrs.update(extra)
            r = creq.get(target, headers=hdrs, impersonate=imp, verify=False,
                         timeout=(15, 120), allow_redirects=True,
                         stream=True, http_version=hv)
            if _is_good_media_response(r):
                print(f"[proxy] CDN OK via {label} for {target[:70]}", flush=True)
                return r, label
            ct = (r.headers.get("Content-Type") or "")[:40]
            st = r.status_code
            print(f"[proxy] CDN retry {label}: status={st} ct={ct!r}", flush=True)
            try: r.close()
            except Exception: pass
        except Exception as e:
            last_error = e
            print(f"[proxy] CDN exception {label}: {e}", flush=True)
            continue

    return None, f"all strategies failed (last_error={last_error})"

# ---------------------------------------------------------------- m3u8 rewrite
M3U8_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
)

def _rewrite_m3u8(content, base_url, referer, cookie=None, provider_id=None):
    if isinstance(content, (bytes, bytearray)):
        content = content.decode("utf-8", "replace")

    def make_proxy(abs_url):
        q = "url=" + quote(abs_url, safe="") + "&referer=" + quote(referer, safe="")
        if cookie:
            q += "&cookie=" + quote(cookie, safe="")
        if provider_id:
            q += "&provider=" + quote(provider_id, safe="")
        return "/?" + q

    out = []
    for line in content.split("\n"):
        s = line.strip()
        if not s:
            out.append(line); continue
        if s.startswith("#"):
            if 'URI="' in s:
                s = re.sub(r'URI="([^"]*)"',
                           lambda m: 'URI="' + make_proxy(urljoin(base_url, m.group(1))) + '"', s)
                out.append(s)
            else:
                out.append(line)
        else:
            out.append(make_proxy(urljoin(base_url, s)))
    return "\n".join(out)

# ---------------------------------------------------------------- providers
PROVIDERS_FILE = os.path.join(BASE_DIR, "providers.json")

def _load_providers():
    if not os.path.exists(PROVIDERS_FILE):
        print(f"[providers] {PROVIDERS_FILE} not found — starting empty", flush=True)
        return {}
    try:
        with open(PROVIDERS_FILE, "r", encoding="utf-8") as f:
            src = json.load(f)
    except Exception as e:
        print(f"[providers] failed to load: {e}", flush=True)
        return {}
    out = {}
    for pid, info in src.items():
        url = (info.get("url") or "").strip()
        try: host = (urlparse(url).hostname or "").lower() if url else ""
        except Exception: host = ""
        out[pid] = {
            "id": pid, "name": info.get("name") or pid, "url": url, "host": host,
            "cdn_hosts": [h.lower() for h in (info.get("cdn_hosts") or []) if h],
        }
    print(f"[providers] loaded {len(out)} providers", flush=True)
    return out

PROVIDERS = _load_providers()

# ---------------------------------------------------------------- plugins
PLUGIN_DIR = os.path.join(BASE_DIR, "providers")
_PLUGINS = {}

def _load_plugins():
    if not os.path.isdir(PLUGIN_DIR):
        print(f"[plugins] {PLUGIN_DIR} not found", flush=True); return
    init_py = os.path.join(PLUGIN_DIR, "__init__.py")
    if not os.path.exists(init_py):
        with open(init_py, "w", encoding="utf-8") as f:
            f.write('"""Provider plugins."""\n')
    if BASE_DIR not in sys.path: sys.path.insert(0, BASE_DIR)
    for entry in sorted(os.listdir(PLUGIN_DIR)):
        if not entry.endswith(".py") or entry.startswith("_"): continue
        modname = entry[:-3]
        try:
            mod = importlib.import_module(f"providers.{modname}")
            cls = getattr(mod, "Provider", None)
            if cls is not None and getattr(cls, "id", None):
                _PLUGINS[cls.id] = cls
                print(f"[plugins] loaded {modname} -> '{cls.id}'", flush=True)
        except Exception as e:
            print(f"[plugins] failed {modname}: {e}", flush=True)

_load_plugins()

# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
        except Exception:
            pass

    def log_message(self, fmt, *args):
        msg = fmt % args
        if ".ts" not in msg and ".m4s" not in msg and ".jpg" not in msg and ".webp" not in msg:
            print(msg, flush=True)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers",
                         "Content-Type, Content-Length, Content-Range, Accept-Ranges")

    def _send_body(self, status, body, content_type, head_only=False):
        if isinstance(body, str): body = body.encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass

    def _send_redirect(self, target_url, head_only=False):
        """302 to CDN — used for ?direct=1 client-side bypass."""
        self.send_response(302)
        self.send_header("Location", target_url)
        self._cors()
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer-when-downgrade")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _is_allowed(self, host, provider_id=None):
        if not host: return False
        host = host.lower()
        if provider_id:
            prov = PROVIDERS.get(provider_id)
            if not prov: return False
            for h in [prov["host"]] + prov.get("cdn_hosts", []):
                if h and (host == h or host.endswith("." + h)): return True
            return False
        for h in ALLOWED_HOSTS:
            if h.startswith("."):
                if host == h[1:] or host.endswith(h): return True
            else:
                if host == h or host.endswith("." + h): return True
        for prov in PROVIDERS.values():
            for h in [prov["host"]] + prov.get("cdn_hosts", []):
                if h and (host == h or host.endswith("." + h)): return True
        return False

    def _serve_html(self, filename, head_only=False):
        safe = os.path.basename(filename)
        path = os.path.join(BASE_DIR, safe)
        if not os.path.exists(path):
            return self._send_body(404, f"Not found: {safe}", "text/plain; charset=utf-8", head_only)
        with open(path, "rb") as f: html = f.read()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head_only:
            try: self.wfile.write(html)
            except (BrokenPipeError, ConnectionResetError): pass

    def _handle_providers(self, path, qs, head_only):
        parts = path.strip("/").split("/")
        if len(parts) == 1:
            data = sorted(
                ({"id": p["id"], "name": p["name"], "url": p["url"],
                  "has_plugin": p["id"] in _PLUGINS} for p in PROVIDERS.values()),
                key=lambda x: x["name"].lower(),
            )
            return self._send_body(200, json.dumps(data, indent=2), "application/json", head_only)

        pid = parts[1]
        prov = PROVIDERS.get(pid)
        if not prov:
            return self._send_body(404, json.dumps({"error": "unknown provider", "id": pid}),
                                   "application/json", head_only)

        action = parts[2] if len(parts) > 2 else None
        if action is None:
            data = dict(prov); data["has_plugin"] = pid in _PLUGINS
            return self._send_body(200, json.dumps(data, indent=2), "application/json", head_only)

        if action not in ("search", "streams"):
            return self._send_body(404, json.dumps({"error": "unknown action"}), "application/json", head_only)

        cls = _PLUGINS.get(pid)
        if cls is None:
            return self._send_body(501, json.dumps({"error": "no plugin", "id": pid}), "application/json", head_only)

        try:
            inst = cls(prov)
            if action == "search":
                result = inst.search(qs.get("q", [""])[0])
            else:
                result = inst.get_streams(qs.get("url", [""])[0])
            return self._send_body(200, json.dumps(result, default=str), "application/json", head_only)
        except NotImplementedError as e:
            return self._send_body(501, json.dumps({"error": "not implemented", "detail": str(e)}),
                                   "application/json", head_only)
        except Exception as e:
            print(f"[plugin] {pid}.{action} error: {e}", flush=True)
            return self._send_body(502, json.dumps({"error": "plugin error", "detail": str(e)}),
                                   "application/json", head_only)

    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header("Content-Length", "0"); self.end_headers()

    def do_HEAD(self): self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)

        # ═══ Fix: don't 400 on favicon ═══
        if path == "/favicon.ico":
            return self._send_body(204, b"", "image/x-icon", head_only)

        if path == "/health":
            return self._send_body(200, '{"status":"ok"}', "application/json", head_only)

        if path == "/providers" or path.startswith("/providers/"):
            return self._handle_providers(path, qs, head_only)

        target = qs.get("url", [None])[0]
        provider_id = qs.get("provider", [None])[0]

        if not target:
            if path in ("/", "/index.html"):
                return self._serve_html(DEFAULT_HTML, head_only)
            if path.endswith(".html"):
                return self._serve_html(path.lstrip("/"), head_only)
            return self._send_body(400, '{"error":"missing url"}', "application/json", head_only)

        target = unquote(target)
        referer = unquote(qs.get("referer", ["https://officialmoviebox.com/"])[0])
        cookie_param = qs.get("cookie", [None])[0]
        direct_flag = qs.get("direct", ["0"])[0] == "1"

        try: host = (urlparse(target).hostname or "").lower()
        except Exception: host = ""

        if not self._is_allowed(host, provider_id=provider_id):
            print(f"[proxy] BLOCK host={host} provider={provider_id} target={target}", flush=True)
            return self._send_body(403, json.dumps({"error": "host not allowed",
                                                    "host": host, "provider": provider_id}),
                                   "application/json", head_only)

        # ═══ Client-side bypass: 302 to CDN (uses user's IP + TLS) ═══
        if direct_flag:
            print(f"[proxy] DIRECT redirect host={host}", flush=True)
            return self._send_redirect(target, head_only)

        is_api = "officialmoviebox.com" in host
        is_media = _guess_content_type_from_path(target) is not None
        rng = self.headers.get("Range")

        # ═══ Media requests: multi-strategy retry ═══
        if is_media:
            r, strategy = _fetch_media_with_retry(target, referer, cookie_param, rng)
            if r is None:
                print(f"[proxy] MEDIA FAIL host={host} strategy={strategy}", flush=True)
                return self._send_body(502, json.dumps({
                    "error": "upstream returned non-video (all strategies failed)",
                    "target": target,
                    "host": host,
                    "referer_sent": referer,
                    "detail": strategy,
                    "hint": "Append &direct=1 to this URL to force a 302 redirect.",
                }), "application/json", head_only)
        else:
            # ═══ API requests: existing single-attempt with v1.1 fallback ═══
            if is_api:
                hdrs = {
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": referer,
                    "Origin": "https://officialmoviebox.com",
                    "x-client-info": '{"timezone":"Asia/Colombo"}',
                    "x-source": "",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                }
                for h in ("x-client-info", "x-source"):
                    v = self.headers.get(h)
                    if v: hdrs[h] = v
            else:
                hdrs = {
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": referer,
                    "Origin": "https://officialmoviebox.com",
                }
                if cookie_param: hdrs["Cookie"] = cookie_param
            if rng: hdrs["Range"] = rng

            try:
                r = creq.get(target, headers=hdrs, impersonate="chrome120",
                             verify=False, timeout=(15, 120), allow_redirects=True,
                             stream=True, http_version="v2")
            except Exception as e:
                print(f"[proxy] UPSTREAM FAIL target={target} error={e}", flush=True)
                try:
                    print(f"[proxy] retrying with HTTP/1.1...", flush=True)
                    r = creq.get(target, headers=hdrs, impersonate="chrome120",
                                 verify=False, timeout=(15, 120), allow_redirects=True,
                                 stream=True, http_version="v1.1")
                except Exception as e2:
                    return self._send_body(502, json.dumps({"error": str(e2), "target": target}),
                                           "application/json", head_only)

        # ── detect non-video responses (safety net; media path already validated) ──
        media_ct = _guess_content_type_from_path(target)
        if media_ct:
            upstream_ct = (r.headers.get("Content-Type") or "").lower()
            bad_content = (
                upstream_ct.startswith("text/html")
                or upstream_ct.startswith("application/json")
                or upstream_ct.startswith("text/plain")
                or upstream_ct.startswith("application/xml")
                or upstream_ct.startswith("text/xml")
            )
            if bad_content or r.status_code >= 400:
                try:
                    err_bytes = r.content[:2048]
                    err_text = err_bytes.decode("utf-8", "replace")
                except Exception as e:
                    err_text = f"(unreadable: {e})"
                try: r.close()
                except Exception: pass

                print(f"[proxy] NON-VIDEO target={target}\n"
                      f"        upstream_status={r.status_code}\n"
                      f"        upstream_ct={upstream_ct}\n"
                      f"        referer_sent={referer}\n"
                      f"        body_preview={err_text[:400]!r}", flush=True)

                return self._send_body(502, json.dumps({
                    "error": "upstream returned non-video",
                    "upstream_status": r.status_code,
                    "upstream_ct": upstream_ct,
                    "target": target,
                    "referer_sent": referer,
                    "body_preview": err_text[:400],
                    "hint": "Append &direct=1 to this URL to force a 302 redirect.",
                }), "application/json", head_only)

        # ── m3u8 rewrite ──
        manifest_ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        is_manifest = (
            urlparse(target).path.lower().endswith(".m3u8")
            or manifest_ct in M3U8_CONTENT_TYPES
        )
        if is_manifest and r.status_code < 400 and not head_only:
            try: raw = r.content
            except Exception as e:
                try: r.close()
                except Exception: pass
                return self._send_body(502, json.dumps({"error": "manifest read failed",
                                                        "detail": str(e)}),
                                       "application/json", head_only)
            try: r.close()
            except Exception: pass
            rewritten = _rewrite_m3u8(raw, target, referer, cookie_param, provider_id)
            body = rewritten.encode("utf-8")
            self.send_response(r.status_code)
            self._cors()
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass
            return

        # ── response headers ──
        upstream_len = r.headers.get("Content-Length")
        encoding = r.headers.get("Content-Encoding")
        compressed = encoding is not None and encoding.lower() not in ("", "identity")
        keep_alive = upstream_len is not None and not compressed

        self.send_response(r.status_code)
        self._cors()

        cdn_ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if cdn_ct.startswith("video/") or cdn_ct == "application/octet-stream":
            out_ct = r.headers.get("Content-Type")
        else:
            out_ct = media_ct or r.headers.get("Content-Type")
        if out_ct: self.send_header("Content-Type", out_ct)

        for k in ("Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
            if k in r.headers: self.send_header(k, r.headers[k])
        if not is_api and "Cache-Control" in r.headers:
            self.send_header("Cache-Control", r.headers["Cache-Control"])
        elif is_api:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
        if keep_alive:
            self.send_header("Content-Length", upstream_len)
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

        if head_only:
            try: r.close()
            except Exception: pass
            return

        try:
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                if chunk: self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        except Exception as e:
            print(f"[stream] write error: {e}", flush=True)
        finally:
            try: r.close()
            except Exception: pass


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except Exception: return "127.0.0.1"
    finally: s.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    index_ok  = os.path.exists(os.path.join(BASE_DIR, "index.html"))
    player_ok = os.path.exists(os.path.join(BASE_DIR, "player.html"))
    on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))

    banner = f"""
✓ MovieBox proxy + provider aggregator (HTTP/2 + CDN retry)
  PORT={port}
  Providers loaded: {len(PROVIDERS)}
  Plugins loaded:   {len(_PLUGINS)} ({', '.join(sorted(_PLUGINS)) or 'none'})
  Serving HTML from: {BASE_DIR}
    - index.html     {"[OK]" if index_ok else "[MISSING]"}
    - player.html    {"[OK]" if player_ok else "[MISSING]"}
"""
    if not on_railway:
        banner += f"\n  Local:   http://localhost:{port}/\n  Network: http://{get_local_ip()}:{port}/\n"
    print(banner, flush=True)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    start_keepalive()
    server.serve_forever()
