"""
fetch_pdf.py  –  Real DOTM Nepal PDF Scraper
──────────────────────────────────────────────
How DOTM actually works (confirmed by live inspection):

  • Category pages list /content/XX/ pages:
      https://dotm.gov.np/category/details-of-printed-licenses/
      https://dotm.gov.np/category/details-of-printed-essential-driver-s-licenses-/

  • Each content page has a "Download" link pointing to:
      https://giwmscdnone.gov.np/media/pdf_upload/<filename>.pdf

This script:
  1. Scrapes both category pages (all pagination)
  2. Visits each content page to find the PDF link
  3. Downloads each PDF into .pdf_cache/
  4. Returns list of local PDF paths for parser.py

Usage:
    python fetch_pdf.py                  # scrape + download all PDFs
    python fetch_pdf.py --limit 5        # newest 5 only (testing)
    python fetch_pdf.py --limit 5 --parse  # download + parse into DB
"""

import os
import re
import time
import logging
import hashlib
import argparse
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urljoin
from html.parser import HTMLParser

log = logging.getLogger(__name__)

BASE_DIR  = Path(__file__).parent
CACHE_DIR = BASE_DIR / ".pdf_cache"

DOTM_BASE = "https://dotm.gov.np"
CDN_BASE  = "https://giwmscdnone.gov.np"

CATEGORY_URLS = [
    f"{DOTM_BASE}/category/details-of-printed-licenses/",
    f"{DOTM_BASE}/category/details-of-printed-essential-driver-s-licenses-/",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9,ne;q=0.8",
    "Referer": "https://dotm.gov.np/",
}

FETCH_DELAY = 1.0   # seconds between requests — be polite


# ─── HTML link extractor ─────────────────────────────────────────────────────

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)


def _fetch_html(url: str) -> str | None:
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=20) as resp:
            charset = "utf-8"
            ct = resp.headers.get("Content-Type", "")
            if "charset=" in ct:
                charset = ct.split("charset=")[-1].strip()
            return resp.read().decode(charset, errors="replace")
    except (HTTPError, URLError, OSError) as e:
        log.warning("Fetch failed [%s]: %s", url, e)
        return None


def _extract_links(html: str) -> list[str]:
    p = LinkParser()
    p.feed(html)
    return p.links


# ─── Step 1: Collect /content/XX/ URLs from category pages ───────────────────

def get_content_page_urls(limit: int | None = None) -> list[str]:
    """Scrape all category pages and collect content page URLs."""
    content_urls: list[str] = []
    seen: set[str] = set()

    for cat_url in CATEGORY_URLS:
        page = 1
        while True:
            url = f"{cat_url}?page={page}" if page > 1 else cat_url
            log.info("Scraping: %s", url)

            html = _fetch_html(url)
            if not html:
                break

            links = _extract_links(html)
            found_new = False

            for href in links:
                if re.match(r"^/content/\d+/", href):
                    full = urljoin(DOTM_BASE, href)
                    if full not in seen:
                        seen.add(full)
                        content_urls.append(full)
                        found_new = True

            if not found_new:
                break

            if f"?page={page + 1}" not in html:
                break

            page += 1
            time.sleep(FETCH_DELAY)

        time.sleep(FETCH_DELAY)

    log.info("Content pages found: %d", len(content_urls))
    return content_urls[:limit] if limit else content_urls


# ─── Step 2: Get PDF URL from a content page ─────────────────────────────────

def extract_pdf_url(content_url: str) -> str | None:
    """Find the PDF download link on a content page."""
    html = _fetch_html(content_url)
    if not html:
        return None

    # Check <a> links
    for href in _extract_links(html):
        if "pdf_upload" in href and href.endswith(".pdf"):
            return href if href.startswith("http") else urljoin(CDN_BASE, href)

    # Fallback: regex search raw HTML
    matches = re.findall(r'https?://[^\s"\'<>]+\.pdf', html)
    for m in matches:
        if "pdf_upload" in m or "giwmscdnone" in m:
            return m

    return None


# ─── Step 3: Download a PDF ───────────────────────────────────────────────────

def download_pdf(pdf_url: str) -> Path | None:
    """Download PDF to cache, skip if already exists."""
    CACHE_DIR.mkdir(exist_ok=True)

    url_hash = hashlib.md5(pdf_url.encode()).hexdigest()[:10]
    local = CACHE_DIR / f"dotm_{url_hash}.pdf"

    if local.exists() and local.stat().st_size > 1024:
        log.debug("Cache hit: %s", local.name)
        return local

    log.info("Downloading: %s", pdf_url)
    try:
        req = Request(pdf_url, headers=HEADERS)
        with urlopen(req, timeout=60) as resp:
            content = resp.read()

        if len(content) < 512 or not content.startswith(b"%PDF"):
            log.warning("Invalid PDF response from: %s", pdf_url)
            return None

        local.write_bytes(content)
        log.info("Saved: %s  (%d KB)", local.name, len(content) // 1024)
        return local

    except (HTTPError, URLError, OSError) as e:
        log.warning("Download failed [%s]: %s", pdf_url, e)
        return None


# ─── Full pipeline ────────────────────────────────────────────────────────────

def scrape_and_download_all(limit: int | None = None) -> list[Path]:
    """
    1. Scrape DOTM category pages for content URLs
    2. Extract PDF links from each content page
    3. Download all PDFs
    Returns list of local PDF paths.
    """
    log.info("══════ DOTM Scraper Starting ══════")
    CACHE_DIR.mkdir(exist_ok=True)

    content_urls = get_content_page_urls(limit=limit)
    if not content_urls:
        log.error("No content pages found.")
        return []

    downloaded: list[Path] = []
    seen_pdfs: set[str] = set()

    for i, url in enumerate(content_urls, 1):
        log.info("[%d/%d] %s", i, len(content_urls), url)
        time.sleep(FETCH_DELAY)

        pdf_url = extract_pdf_url(url)
        if not pdf_url or pdf_url in seen_pdfs:
            log.warning("  No PDF found or duplicate: %s", url)
            continue

        seen_pdfs.add(pdf_url)
        time.sleep(FETCH_DELAY)

        local = download_pdf(pdf_url)
        if local:
            downloaded.append(local)

    log.info("══════ Done: %d PDFs ══════", len(downloaded))
    return downloaded


def get_cached_pdfs() -> list[Path]:
    if not CACHE_DIR.exists():
        return []
    return sorted(CACHE_DIR.glob("dotm_*.pdf"), reverse=True)


def download_latest_pdf(output_path=None) -> Path | None:
    """Compat shim for main.py — scrape + return first PDF."""
    pdfs = scrape_and_download_all(limit=10)
    if pdfs:
        return pdfs[0]
    cached = get_cached_pdfs()
    return cached[0] if cached else None


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    ap = argparse.ArgumentParser(description="DOTM Nepal PDF Scraper")
    ap.add_argument("--limit", type=int, default=None,
                    help="Max content pages to scrape")
    ap.add_argument("--parse", action="store_true",
                    help="Also parse downloaded PDFs into database")
    args = ap.parse_args()

    pdfs = scrape_and_download_all(limit=args.limit)

    if not pdfs:
        print("\n✗ No PDFs downloaded.")
    else:
        print(f"\n✓ Downloaded {len(pdfs)} PDF(s):")
        for p in pdfs:
            print(f"   {p}")

        if args.parse:
            print("\nParsing into database…")
            import parser as pdf_parser
            import db
            db.init_db()
            total = 0
            for pdf_path in pdfs:
                try:
                    n = pdf_parser.parse_and_store(pdf_path)
                    total += n
                    print(f"   ✓ {pdf_path.name}: {n} records")
                except Exception as e:
                    print(f"   ✗ {pdf_path.name}: {e}")
            print(f"\n✓ Total records imported: {total}")