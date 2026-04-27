"""
fetch_pdf.py  –  DOTM Nepal License PDF Downloader
────────────────────────────────────────────────────
Downloads license PDFs from official DOTM CDN.

To add more offices later:
  1. Go to the office page on dotm.gov.np
  2. Right-click Download button → Copy Link Address
  3. Add the URL to DIRECT_PDF_URLS below

Usage:
    python fetch_pdf.py           # download + parse into DB
    python fetch_pdf.py --only-download   # download only
"""

import logging
import hashlib
import argparse
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

log = logging.getLogger(__name__)

BASE_DIR  = Path(__file__).parent
CACHE_DIR = BASE_DIR / ".pdf_cache"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
    "Referer":    "https://dotm.gov.np/",
}

# ══════════════════════════════════════════════════════
#   ADD / REMOVE PDF URLS HERE AS DOTM PUBLISHES THEM
# ══════════════════════════════════════════════════════

DIRECT_PDF_URLS = {
    "Chabahil":   "https://giwmscdnone.gov.np/media/pdf_upload/Chabahil%20Printed%20License%20Card%20List_5siwppy.pdf",
    "Radheradhe": "https://giwmscdnone.gov.np/media/pdf_upload/Radhe%20radhe%20Printed%20License%20175598_kmfke54.pdf",

    # Add more offices below when you get the URLs:
    # "Thulobharyang": "https://giwmscdnone.gov.np/media/pdf_upload/...",
    # "Arghakachi":    "https://giwmscdnone.gov.np/media/pdf_upload/...",
    # "Okhaldhunga":   "https://giwmscdnone.gov.np/media/pdf_upload/...",
    # "Rajbiraj":      "https://giwmscdnone.gov.np/media/pdf_upload/...",
    # "Itahari":       "https://giwmscdnone.gov.np/media/pdf_upload/...",
    # "Jhapa":         "https://giwmscdnone.gov.np/media/pdf_upload/...",
    # "Dhankuta":      "https://giwmscdnone.gov.np/media/pdf_upload/...",
    # "Dumre":         "https://giwmscdnone.gov.np/media/pdf_upload/...",
    # "Kawasoti":      "https://giwmscdnone.gov.np/media/pdf_upload/...",
    # "Gaur":          "https://giwmscdnone.gov.np/media/pdf_upload/...",
    # "Parasi":        "https://giwmscdnone.gov.np/media/pdf_upload/...",
}

# ══════════════════════════════════════════════════════


def download_pdf(office: str, url: str) -> Path | None:
    """Download one PDF, skip if already cached."""
    CACHE_DIR.mkdir(exist_ok=True)

    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    local    = CACHE_DIR / f"{office.lower()}_{url_hash}.pdf"

    if local.exists() and local.stat().st_size > 1024:
        log.info("  ✓ [%s] Already cached: %s", office, local.name)
        return local

    log.info("  ⬇ [%s] Downloading…", office)
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=60) as resp:
            data = resp.read()

        if len(data) < 512 or not data.startswith(b"%PDF"):
            log.warning("  ✗ [%s] Invalid PDF (%d bytes)", office, len(data))
            return None

        local.write_bytes(data)
        log.info("  ✓ [%s] Saved: %s (%d KB)", office, local.name, len(data) // 1024)
        return local

    except (HTTPError, URLError, OSError) as e:
        log.warning("  ✗ [%s] Failed: %s", office, e)
        return None


def download_all() -> list[Path]:
    """Download all PDFs in DIRECT_PDF_URLS."""
    print(f"\n📥 Downloading {len(DIRECT_PDF_URLS)} office PDF(s)…\n")

    downloaded = []
    for office, url in DIRECT_PDF_URLS.items():
        path = download_pdf(office, url)
        if path:
            downloaded.append(path)

    print(f"\n✅ Downloaded: {len(downloaded)}/{len(DIRECT_PDF_URLS)} PDFs\n")
    return downloaded


def get_cached_pdfs() -> list[Path]:
    if not CACHE_DIR.exists():
        return []
    return sorted(CACHE_DIR.glob("*.pdf"), reverse=True)


def download_latest_pdf(output_path=None) -> Path | None:
    """Compat shim for main.py"""
    pdfs = download_all()
    return pdfs[0] if pdfs else None


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    ap = argparse.ArgumentParser(description="DOTM License PDF Downloader")
    ap.add_argument("--only-download", action="store_true",
                    help="Download PDFs only, do not parse into DB")
    args = ap.parse_args()

    pdfs = download_all()

    if not pdfs:
        print("✗ No PDFs downloaded. Check URLs or internet connection.")
    else:
        for p in pdfs:
            print(f"   📄 {p.name}  ({p.stat().st_size // 1024} KB)")

        if not args.only_download:
            print("\n📊 Parsing into database…\n")
            import parser as pdf_parser
            import db

            db.init_db()
            total = 0
            for pdf_path in pdfs:
                try:
                    n = pdf_parser.parse_and_store(pdf_path)
                    total += n
                    print(f"   ✓ {pdf_path.stem}: {n:,} records")
                except Exception as e:
                    print(f"   ✗ {pdf_path.stem}: {e}")

            print(f"\n🎉 Total records in DB: {total:,}")