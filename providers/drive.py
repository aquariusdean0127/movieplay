"""MoviesDrive provider plugin (v9 — cloud-link extraction from every source)."""

import html
import re
from urllib.parse import quote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

from providers._hubcloud import find_cloud_links, resolve_cloud_url

SEARCH_SELECTORS = [
    "h2.entry-title a[href]", "h3.entry-title a[href]",
    "h2.post-title a[href]", "h3.post-title a[href]",
    ".post-title a[href]", ".entry-title a[href]",
    ".movie-title a[href]", ".title a[href]",
    "article h2 a[href]", "article h3 a[href]",
]

REJECT_URL_HINTS = (
    "/category/", "/tag/", "/author/", "/feed/", "/page/",
    "/wp-content/", "/wp-admin/", "/wp-json/", "/xmlrpc",
    "?s=", "/#", "javascript:", "mailto:",
    "/dmca", "/privacy", "/contact", "/about",
    "/telegram", "/disclaimer", "/how-to-",
)


class Provider:
    id = "drive"

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

        debug = {"sources": {}, "cloud_links": [], "resolved": []}
        texts = []

        # 1. plain fetch
        try:
            r = self._get(page_url)
            texts.append(("page", r.text))
            debug["sources"]["page_bytes"] = len(r.text)
        except Exception as e:
            debug["sources"]["page_error"] = str(e)

        # 2. with site referer (hotlink protection bypass)
        try:
            r = self.session.get(page_url, headers={
                "Referer": self.base,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
            }, timeout=45, allow_redirects=True)
            texts.append(("page-referer", r.text))
        except Exception:
            pass

        # 3. ?download=1 variants
        for suffix in ("?download=1", "?dl=1", "?download=true"):
            try:
                r = self._get(page_url + suffix)
                texts.append((f"page{suffix}", r.text))
            except Exception:
                pass

        # 4. wp-json post content + excerpt
        try:
            path = urlparse(page_url).path.strip("/")
            slug = path.split("/")[-1] if path else ""
            if slug:
                r = self._get(self.base + "wp-json/wp/v2/posts?" +
                              urlencode({"slug": slug}))
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        rendered = data[0].get("content", {}).get("rendered", "")
                        excerpt = data[0].get("excerpt", {}).get("rendered", "")
                        if rendered:
                            texts.append(("wp-content", rendered))
                            debug["sources"]["wp_bytes"] = len(rendered)
                        if excerpt:
                            texts.append(("wp-excerpt", excerpt))
        except Exception:
            pass

        # collect cloud links from every source
        cloud_links = set()
        for name, text in texts:
            found = find_cloud_links(text)
            if found:
                cloud_links |= found
                debug["sources"][name] = {"cloud_links": len(found)}

        # search-recover URLs are hubcloud search pages; fetch them for more
        search_recover = [u for u in cloud_links if "search-recover" in u]
        for link in search_recover[:2]:
            try:
                r = self._get(link)
                extra = find_cloud_links(r.text)
                cloud_links |= extra
                if extra:
                    debug["sources"].setdefault("search-recover", []).append(
                        {"url": link[:120], "found": len(extra)})
            except Exception:
                pass

        # drop the search-recover URLs themselves; only real file pages remain
        file_links = {u for u in cloud_links if "search-recover" not in u}
        debug["cloud_links"] = sorted(file_links)[:15]
        print(f"[drive] {len(file_links)} file cloud links on {page_url[:80]}",
              flush=True)

        # resolve every cloud link
        streams = []
        seen_urls = set()
        for link in file_links:
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

        if not streams:
            debug["hint"] = (
                "No resolvable cloud links. The download widget may be JS-only. "
                "If debug.cloud_links is non-empty but resolved is empty, the "
                "site changed its mirror IDs — check the first entry."
            )

        print(f"[drive] get_streams → {len(streams)} streams", flush=True)
        return {"streams": streams, "page": page_url, "debug": debug}
