#!/usr/bin/env python3
"""Conservative PSPKG catalog updater for PS5 Library.

Fetches publicly accessible PSPKG pages. Never manufactures download URLs,
never removes known working entries, and does not attempt to bypass logins or CAPTCHA.
"""
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urldefrag

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "PS5_Arabic_PSPKG_catalog.json"
PENDING = ROOT / "ps5_arabic_needs_download_links.json"
BASE = "https://pspkg.com"
LISTING = f"{BASE}/arabic-games/"
MAX_PAGES = 8
MAX_GAMES = 180
GAME_URL = re.compile(r"/arabic-games/[^/?#]+-\d+\.html$", re.I)
EXTERNAL_HOSTS = ("vik1ngfile.site", "vikingfile", "datanodes", "1fichier.com", "pixeldrain.com", "gofile.io", "mediafire.com", "mega.nz")
EXCLUDE_LABEL = re.compile(r"backport|dlcs?|patch|updates?|تحديث|إضافات|الاضافات", re.I)
GAME_LABEL = re.compile(r"\bgame\b|العبة|اللعبة|\bpkg\b", re.I)
USER_AGENT = "Mozilla/5.0 (compatible; PSPKGCatalogSync/1.0; daily catalog maintenance)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8,ar;q=0.5"})


def fetch(url, params=None):
    try:
        r = SESSION.get(url, params=params, timeout=25)
        if r.status_code != 200:
            print(f"WARN: site returned HTTP {r.status_code} for {r.url}", file=sys.stderr)
            return None
        if "text/html" not in r.headers.get("Content-Type", "text/html"):
            return None
        return BeautifulSoup(r.text, "html.parser")
    except requests.RequestException as ex:
        print(f"WARN: fetching {url} failed: {type(ex).__name__}", file=sys.stderr)
        return None


def clean_title(s):
    s = re.sub(r"\s*\(v\d[\d.]+\)\s*$", "", s, flags=re.I)
    return " ".join(unicodedata.normalize("NFKC", s).casefold().split())


def photo(soup):
    for key in ("og:image", "twitter:image"):
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag and tag.get("content"):
            return urljoin(BASE, tag["content"])
    return ""


def visible_title(soup, fallback):
    h = soup.find("h1")
    if h:
        return h.get_text(" ", strip=True)
    tag = soup.find("meta", attrs={"property": "og:title"})
    if tag and tag.get("content"):
        return tag["content"].split(" | ")[0].strip()
    return fallback


def find_games():
    found = {}
    for page in range(1, MAX_PAGES + 1):
        soup = fetch(LISTING, params={"filters[]": "ps5", "page": str(page)})
        if soup is None:
            if page == 1:
                raise RuntimeError("Cannot access PSPKG public listing; catalog unchanged")
            break
        added = 0
        for tag in soup.find_all("a", href=True):
            link = urldefrag(urljoin(BASE, tag["href"])).url
            parts = urlsplit(link)
            if parts.netloc not in ("pspkg.com", "www.pspkg.com") or not GAME_URL.search(parts.path):
                continue
            # Filter out PS4 / PS3 entries from popular/suggested areas.
            snippet = " ".join(tag.parent.get_text(" ", strip=True).split()) if tag.parent else ""
            context = " ".join((snippet + " " + (tag.parent.parent.get_text(" ", strip=True) if tag.parent and tag.parent.parent else "")).split())
            if "PS4" in context and "PS5" not in context:
                continue
            if link not in found:
                found[link] = tag.get_text(" ", strip=True) or parts.path.rsplit("/", 1)[-1].split("-")[0]
                added += 1
        print(f"Listing page {page}: {added} new game pages")
        if added == 0 or len(found) >= MAX_GAMES:
            break
        time.sleep(0.9)
    return dict(list(found.items())[:MAX_GAMES])


def is_download_host(url):
    p = urlsplit(url)
    h = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or not h:
        return False
    return any(h == x or h.endswith("." + x) or x in h for x in EXTERNAL_HOSTS) or p.path.lower().endswith(".pkg")


def public_game_download_links(soup):
    """Only extract visibly labelled *game* links (not DLC/backport) from HTML.

    For member-only, client-side or CAPTCHA-protected links, returns none rather
    than guessing. A link here may still lead to a download host's landing page.
    """
    sources = []
    for a in soup.find_all("a", href=True):
        href = urljoin(BASE, a["href"].strip())
        if not is_download_host(href):
            continue
        # Look only at a small local section to avoid unrelated site navigation.
        node = a
        nearby = []
        for _ in range(3):
            node = node.parent
            if not node or node.name in ("body", "html"):
                break
            txt = node.get_text(" ", strip=True)
            if len(txt) <= 350:
                nearby.append(txt)
        label = " | ".join(nearby)
        if not GAME_LABEL.search(label) or EXCLUDE_LABEL.search(label):
            continue
        if href not in sources:
            sources.append(href)
    return sources


def catalog_entry(url, title, cover, links):
    slug = urlsplit(url).path.rsplit("/", 1)[-1].rsplit(".html", 1)[0]
    files = [{"url": u, "name": f"{title} - Game PKG"} for u in links]
    return {
        "id": f"pspkg-ps5-ar-{slug}",
        "title": title,
        "kind": "game", "platform": "ps5", "format": "fpkg",
        "cover": cover,
        "description": "PSPKG PS5 Arabic game. Hosting site may require verification. Confirm PKG format and firmware compatibility before installing.",
        "files": files,
        "source_sets": [{"host": urlsplit(u).hostname or "Download host", "files": [f]} for u, f in zip(links, files)],
    }


def main():
    if not CATALOG.is_file():
        raise RuntimeError("Original catalog not found; refusing to overwrite anything")
    original = json.loads(CATALOG.read_text(encoding="utf-8"))
    entries = original.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("Catalog must contain an entries list")
    pages = find_games()
    if not pages:
        raise RuntimeError("No game pages discovered; refusing to overwrite catalog")
    old_title_keys = {clean_title(e.get("title", "")) for e in entries}
    old_ids = {e.get("id") for e in entries}
    pending = []
    added = 0
    refreshed = 0
    for url, fallback in pages.items():
        soup = fetch(url)
        if soup is None:
            continue
        title = visible_title(soup, fallback)
        cover = photo(soup)
        links = public_game_download_links(soup)
        matching = next((e for e in entries if clean_title(e.get("title", "")) == clean_title(title)), None)
        if matching:
            # Preserve personally verified file URLs; enrich only missing fields.
            if not matching.get("cover") and cover:
                matching["cover"] = cover
                refreshed += 1
            if not matching.get("files") and links:
                candidate = catalog_entry(url, title, cover, links)
                matching["files"] = candidate["files"]
                matching["source_sets"] = candidate["source_sets"]
                refreshed += 1
        elif links:
            entry = catalog_entry(url, title, cover, links)
            if entry["id"] not in old_ids and clean_title(title) not in old_title_keys:
                entries.append(entry)
                old_ids.add(entry["id"])
                old_title_keys.add(clean_title(title))
                added += 1
        else:
            pending.append({"title": title, "pspkg_page": url, "reason": "No publicly accessible verified Game PKG link"})
        time.sleep(0.5)
    # Keep any already working entry even if PSPKG changes its indexing.
    text = json.dumps(original, ensure_ascii=False, indent=2) + "\n"
    if CATALOG.read_text(encoding="utf-8") != text:
        CATALOG.write_text(text, encoding="utf-8")
    pending_doc = {
        "note": "Discovery only. Items below were NOT added to PS5 Library; subscription links must be supplied or exposed to an authorized session.",
        "last_scan_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pending": pending,
    }
    # Avoid committing a timestamp-only change every day.
    if PENDING.is_file():
        previous = json.loads(PENDING.read_text(encoding="utf-8"))
        if previous.get("pending") == pending:
            pending_doc["last_scan_utc"] = previous.get("last_scan_utc", pending_doc["last_scan_utc"])
    pending_text = json.dumps(pending_doc, ensure_ascii=False, indent=2) + "\n"
    if not PENDING.is_file() or PENDING.read_text(encoding="utf-8") != pending_text:
        PENDING.write_text(pending_text, encoding="utf-8")
    print(f"Found {len(pages)} game pages; added {added}, enriched {refreshed}, pending {len(pending)}. Preserved existing URLs.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
