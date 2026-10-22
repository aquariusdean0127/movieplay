"""HDHub4u provider plugin (v11 — HubCloud/GadgetsWeb chain)."""

import html
import re
from urllib.parse import quote, urlencode, urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

from providers._hubcloud import find_cloud_links, resolve_cloud_url

SEARCH_SELECTORS = [
    "article h2 a[href]", "article h3 a[href]",
    "h2.entry-title a[href]", "h3.entry-title a[href]",
    "h2.post-title a[href]", "h3.post-title a[href]",
    ".post-title a[href]", ".entry-title a[href]",
]

REJECT_URL_HINTS = (
    "/category/", "/tag/", "/author/", "/feed/", "/page/",
    "/wp-content/", "/wp-admin/", "/wp-json/", "/xmlrpc",
    "?s=", "/#", "javascript:", "mailto:",
    "/app/", "/dmca", "/privacy", "/contact", "/about",
    "/how-to-", "/telegram", "/disclaimer", "/request-a-movie",
    "/join-our-group", "/slider/", "/search.html",
)


class Provider:
    id = "hdhub"

    def __init__(self, info):
        self.info = info
        self.base = (info.get("url") or "").rstrip("/") + "/"
        self.session = creq.Session(impersonate="chrome120", verify=False)

    def _get(self, url, **kw):
        return self.session.get(url, timeout=45, allow_redirects=True, **kw)

    # ---------- search (unchanged) ----------
    def search(self, q):
        q = (q or "").strip()
        if not q:
            return {"results": []}
        results = self._wp_rest_search(q)
        if results:
            return {"query": q, "results": results}
        results = self._scrape_page(self.base + "search.html?" + urlencode({"s": q}))
        if results:
            return {"query": q, "results": results}
        results = self._scrape_page(self.base + "?s=" + quote(q))
        return {"query": q, "results": results}

    def _wp_rest_search(self, q):
        try:
            url = self.base + "wp-json/wp/v2/search?" + urlencode({
                "search": q, "per_page": "50", "subtype": "post",
            })
            r = self._get(url)
            if r.status_code != 200:
                return []
            data = r.json()
            if not isinstance(data, list):
                return []
        except Exception:
            return []
        items = []
        for item in data:
            title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
            title = html.unescape(title)
            url = item.get("url") or ""
            if title and url and self._accept(url, title):
                items.append({"title": title, "url": url, "type": "movie"})
        q_lower = q.lower()
        q_words = [w for w in re.split(r"\W+", q_lower) if len(w) >= 3]
        matches = [it for it in items
                   if q_lower in it["title"].lower()
                   or all(w in it["title"].lower() for w in q_words)]
        return matches or items

    def _scrape_page(self, url):
        try:
            r = self._get(url)
            r.raise_for_status()
        except Exception:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        results, seen = [], set()
        for sel in SEARCH_SELECTORS:
            for a in soup.select(sel):
                href = (a.get("href") or "").strip()
                title = a.get_text(" ", strip=True)
                if not self._accept(href, title) or href in seen:
                    continue
                seen.add(href)
                results.append({"title": title,
                                "url": urljoin(self.base, href),
                                "type": "movie"})
            if results:
                return results
        return results

    def _accept(self, href, title):
        if not href or not title:
            return False
        if len(title) < 4 or len(title) > 200:
            return False
        low = href.lower()
        if any(hint in low for hint in REJECT_URL_HINTS):
            return False
        if not low.startswith(("http://", "https://")):
            return True
        base_host = self.base.split("//", 1)[-1].split("/")[0]
        return base_host in low

    # ---------- get_streams ----------
    def get_streams(self, page_url):
        page_url = (page_url or "").strip()
        if not page_url:
            return {"streams": []}

        debug = {"cloud_links": [], "resolved": []}

        try:
            r = self._get(page_url)
            r.raise_for_status()
        except Exception as e:
            return {"streams": [], "page": page_url,
                    "debug": {"error": str(e)}}

        cloud_links = find_cloud_links(r.text)
        debug["cloud_links"] = sorted(cloud_links)[:12]
        print(f"[hdhub] {len(cloud_links)} cloud links on {page_url[:80]}", flush=True)

        streams = []
        seen_urls = set()
        for link in cloud_links:
            try:
                results = resolve_cloud_url(self.session, link, referer=page_url)
                debug["resolved"].append({
                    "input": link[:120],
                    "streams": len(results),
                    "first": results[0]["url"][:140] if results else None,
                })
                for s in results:
                    if s["url"] not in seen_urls:
                        seen_urls.add(s["url"])
                        streams.append(s)
            except Exception as e:
                debug["resolved"].append({"input": link[:120],
                                          "error": str(e)[:120]})

        print(f"[hdhub] get_streams → {len(streams)} streams", flush=True)
        return {"streams": streams, "page": page_url, "debug": debug}
