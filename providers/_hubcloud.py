"""Shared HubCloud / Gamerxyt / GadgetsWeb / HubDrive / GDflix resolver.

Confirmed flow:
  hubcloud.<tld>/drive/<id>  → gamerxyt generator → FSLv2 / FSL / Pixel / 10Gbps
  gadgetsweb.xyz/?id=...     → decrypt token → hubcloud drive URL
  hubdrive.<tld>/file/<id>   → POST /ajax.php?ajax=download {id} → JSON file URL
"""

import base64
import datetime
import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

CLOUD_HOST_PATTERNS = [
    r"hubcloud\.\w+",
    r"gadgetsweb\.\w+",
    r"hubdrive\.\w+",
    r"hubcdn\.\w+",
    r"hubstream\.\w+",
    r"gdflix\.\w+",
    r"driveleech\.\w+",
    r"filepress\.\w+",
    r"gdtot\.\w+",
    r"kolop\.\w+",
    r"pixeldrain\.\w+",
]

DIRECT_HOST_PATTERNS = [
    r"video-downloads\.googleusercontent\.com",
    r"gigabytes\.icu",
    r"airbender\.buzz",
    r"\.mkv(\?|$)",
    r"\.mp4(\?|$)",
]


def find_cloud_links(text):
    urls = set()
    if not text:
        return urls
    for m in re.finditer(r"https?://[^\s\"'<>()\\]+", text):
        u = m.group(0).rstrip(".,;)\"'")
        if any(re.search(p, u, re.I) for p in CLOUD_HOST_PATTERNS):
            urls.add(u.replace("&amp;", "&"))
    return urls


def resolve_cloud_url(session, url, referer=None):
    host = urlparse(url).netloc.lower()

    if "hubdrive" in host:
        return _resolve_hubdrive(session, url)
    if "hubcloud" in host and "/drive/" in url:
        return _resolve_hubcloud(session, url, referer)
    if "gadgetsweb" in host:
        return _resolve_gadgetsweb(session, url, referer)
    return _follow_shortener(session, url, referer)


# ----------------------------------------------------------------- hubcloud
def _resolve_hubcloud(session, landing_url, referer=None):
    hdrs = {"Referer": referer} if referer else {}
    try:
        r = session.get(landing_url, headers=hdrs, timeout=45, allow_redirects=True)
    except Exception as e:
        print(f"[hubcloud] landing GET failed: {e}", flush=True)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    gen_url = None
    a = soup.find("a", id="download")
    if a and a.get("href"):
        gen_url = a["href"].strip()
    else:
        m = re.search(r"https://gamerxyt\.com/hubcloud\.php\S+", r.text)
        if m:
            gen_url = m.group(0).replace("'", "").replace('"', "").replace(";", "")

    if not gen_url:
        print(f"[hubcloud] no generator link on {landing_url[:80]}", flush=True)
        return []

    gen_url = gen_url.replace("&amp;", "&")
    print(f"[hubcloud] generator: {gen_url[:120]}", flush=True)

    try:
        r2 = session.get(gen_url, headers={"Referer": landing_url},
                         timeout=45, allow_redirects=True)
    except Exception as e:
        print(f"[hubcloud] generator GET failed: {e}", flush=True)
        return []

    soup2 = BeautifulSoup(r2.text, "html.parser")
    out = []
    minute = datetime.datetime.now().minute

    a = soup2.find("a", id="s3")
    if a and a.get("href"):
        out.append({"url": a["href"] + f"_1{minute}",
                    "quality": "Auto", "format": "MKV", "label": "FSLv2"})

    a = soup2.find("a", id="fsl")
    if a and a.get("href"):
        out.append({"url": a["href"] + f"1{minute}",
                    "quality": "Auto", "format": "MKV", "label": "FSL"})

    m = re.search(r'var pxl = "([^"]+)"', r2.text)
    pxl = m.group(1) if m else None
    if not pxl:
        a = soup2.find("a", id="pxl-1")
        if a and a.get("href"):
            pxl = a["href"]
    if pxl:
        pm = re.search(r"pixeldrain\.\w+/u/([A-Za-z0-9]+)", pxl)
        if pm:
            pxl = f"https://pixeldrain.com/api/file/{pm.group(1)}?download"
        out.append({"url": pxl, "quality": "Auto", "format": "", "label": "Pixel"})

    a = soup2.find("a", href=re.compile(r"bzzhr\.co"))
    if a and a.get("href"):
        out.append({"url": a["href"], "quality": "Auto",
                    "format": "", "label": "Buzz"})

    a = soup2.find("a", href=re.compile(r"pixel\.hubcloud\.cx"))
    if a and a.get("href"):
        try:
            head = session.head(a["href"], headers={"Referer": gen_url},
                                allow_redirects=True, timeout=30)
            final = head.url
            if final and final != a["href"]:
                out.insert(0, {"url": final, "quality": "Auto",
                               "format": "MP4", "label": "10Gbps/Google"})
            else:
                out.append({"url": a["href"], "quality": "Auto",
                            "format": "", "label": "10Gbps"})
        except Exception:
            out.append({"url": a["href"], "quality": "Auto",
                        "format": "", "label": "10Gbps"})

    print(f"[hubcloud] {len(out)} mirrors from {landing_url[:60]}", flush=True)
    return out


# --------------------------------------------------------------- gadgetsweb
def _resolve_gadgetsweb(session, url, referer=None):
    try:
        session.get(url, timeout=30)
    except Exception:
        pass
    try:
        session.get("https://gadgetsweb.xyz/homelander/", timeout=30)
    except Exception:
        pass
    try:
        r = session.get(url, timeout=30)
    except Exception:
        return []

    m = re.search(r"s\('o','([^']+)'", r.text)
    if not m:
        return []
    token = m.group(1)

    try:
        s1 = base64.b64decode(token).decode("utf-8")
        s2 = base64.b64decode(s1).decode("utf-8")
        s3 = _rot13(s2)
        padded = s3 + "=" * ((4 - len(s3) % 4) % 4)
        s4 = base64.b64decode(padded).decode("utf-8")
        data = json.loads(s4)
        hub_b64 = data.get("o")
        if not hub_b64:
            return []
        next_url = base64.b64decode(hub_b64).decode("utf-8")
    except Exception as e:
        print(f"[gadgetsweb] decrypt failed: {e}", flush=True)
        return []

    print(f"[gadgetsweb] → {next_url[:100]}", flush=True)
    return _resolve_hubcloud(session, next_url, referer)


# ----------------------------------------------------------------- hubdrive
def _resolve_hubdrive(session, file_url):
    """hubdrive.<tld>/file/<id> → POST /ajax.php?ajax=download {id}.

    Reads raw bytes with Accept-Encoding: identity to bypass the curl_cffi
    decompression bug that returns 0 bytes despite Content-Length > 0.
    """
    m = re.search(r"/file/([^/?#]+)", file_url)
    if not m:
        return []
    fid = m.group(1)
    origin = f"{urlparse(file_url).scheme}://{urlparse(file_url).netloc}"

    # Prime cookies + scrape down-id (some deploys use a different id)
    ids = [fid]
    try:
        page = session.get(file_url, timeout=30)
        dm = re.search(r'id=["\']down-id["\'][^>]*>([^<]+)<', page.text)
        if dm and dm.group(1).strip():
            alt = dm.group(1).strip()
            if alt and alt != fid:
                ids.insert(0, alt)
    except Exception:
        pass

    for cid in ids:
        result = _hubdrive_ajax_attempt(session, origin, file_url, cid)
        if result:
            return result

    print(f"[hubdrive] ajax exhausted for {fid}", flush=True)
    return []


def _hubdrive_ajax_attempt(session, origin, file_url, cid):
    """Try every reasonable combination; return the first working stream."""
    base_headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": file_url,
        "Origin": origin,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Encoding": "identity",   # ← key: skip compression
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    bodies = [
        {"id": cid},
        {"ajax": "download", "id": cid},
        f"id={cid}",
    ]
    urls = [origin + "/ajax.php?ajax=download", origin + "/ajax.php"]

    for body in bodies:
        for url in urls:
            try:
                resp = session.post(url, data=body, headers=base_headers,
                                    timeout=30, allow_redirects=False)
            except Exception as e:
                print(f"[hubdrive] POST err {url[-30:]}: {e}", flush=True)
                continue

            raw = _read_body(resp)
            clen = resp.headers.get("Content-Length") or "?"
            label = list(body)[0] if isinstance(body, dict) else "str"
            print(
                f"[hubdrive] {url.split('//')[-1]} body={label} "
                f"→ {resp.status_code} clen={clen} got={len(raw)}b",
                flush=True,
            )

            # 302 → file
            loc = resp.headers.get("Location")
            if loc and loc.startswith("http") and any(x in loc for x in (".mkv", ".mp4", "googleusercontent")):
                return [{"url": loc, "quality": "Auto",
                         "format": "MP4", "label": "HubDrive"}]

            # JSON response
            if raw:
                txt = raw.decode("utf-8", "replace").strip()
                print(f"[hubdrive]   body: {txt[:120]!r}", flush=True)
                if txt.startswith("{"):
                    try:
                        j = json.loads(txt)
                        if str(j.get("code")) == "200":
                            u = j.get("file") or j.get("url") or j.get("link")
                            if u:
                                return [{"url": u, "quality": "Auto",
                                         "format": "MP4", "label": "HubDrive"}]
                    except Exception:
                        pass

    return None


def _read_body(resp):
    """Read response bytes with multiple fallbacks for curl_cffi edge cases."""
    for getter in (
        lambda: resp.content,
        lambda: resp.raw.read(),
        lambda: (resp.text or "").encode("utf-8"),
    ):
        try:
            data = getter()
            if data:
                return data
        except Exception:
            continue
    return b""


# ------------------------------------------------------------------ generic
def _follow_shortener(session, url, referer=None):
    hdrs = {"Referer": referer} if referer else {}
    try:
        r = session.get(url, headers=hdrs, timeout=45, allow_redirects=True)
    except Exception:
        return []

    ct = (r.headers.get("Content-Type") or "").lower()
    if any(k in ct for k in ("video/", "application/octet-stream")):
        return [{"url": str(r.url), "quality": "Auto",
                 "format": "MP4", "label": "Direct"}]

    out = []
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(re.search(p, href, re.I) for p in DIRECT_HOST_PATTERNS):
            out.append({"url": href, "quality": "Auto",
                        "format": "MP4", "label": "Direct"})
    return out


def _rot13(s):
    out = []
    for ch in s:
        if "a" <= ch <= "z":
            out.append(chr((ord(ch) - ord("a") + 13) % 26 + ord("a")))
        elif "A" <= ch <= "Z":
            out.append(chr((ord(ch) - ord("A") + 13) % 26 + ord("A")))
        else:
            out.append(ch)
    return "".join(out)
