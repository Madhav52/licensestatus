"""
parser.py  –  FAST DOTM Nepal License PDF Parser
──────────────────────────────────────────────────
PDF format per line:
    185 AADIT BISWOKARMA 04-06-89128145 A CHABAHIL 2026-FEB-06
    [SN] [NAME]          [LICENSE_NO]  [CAT] [OFFICE]  [DATE]

Usage:
    python parser.py
    python parser.py --pdf license.pdf
    python parser.py --pdf license.pdf --workers 8
"""

import re
import sys
import time
import logging
import argparse
from pathlib import Path
from datetime import date, datetime
from concurrent.futures import ProcessPoolExecutor, as_completed

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
PDF_PATH = BASE_DIR / "license.pdf"

# ── Regex patterns ───────────────────────────────────────────────────────────

# License number: XX-XX-XXXXXXXX
LICENSE_RE = re.compile(r'\b(\d{1,2}-\d{2,3}-\d{5,10})\b')

# Date formats: 2026-FEB-06 or 2081-05-15 or 2026/02/06
DATE_RE = re.compile(
    r'\b(\d{4}[-/](?:\d{1,2}|JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[-/]\d{1,2})\b',
    re.IGNORECASE
)

# Category codes: A, B, K, C, D, etc. possibly comma/slash separated
CAT_RE = re.compile(r'\b([A-GKa-gk](?:\s*[,/]\s*[A-GKa-gk])*)\b')

# Month name → number map for normalization
MONTH_MAP = {
    'JAN': '01', 'FEB': '02', 'MAR': '03', 'APR': '04',
    'MAY': '05', 'JUN': '06', 'JUL': '07', 'AUG': '08',
    'SEP': '09', 'OCT': '10', 'NOV': '11', 'DEC': '12'
}


def _normalize_date(raw: str) -> str:
    """Convert 2026-FEB-06 → 2026-02-06, keep numeric dates as-is."""
    raw = raw.strip()
    for mon, num in MONTH_MAP.items():
        raw = raw.upper().replace(f'-{mon}-', f'-{num}-').replace(f'/{mon}/', f'/{num}/')
    return raw.replace('/', '-')


def _parse_line(line: str) -> dict | None:
    """
    Parse a single PDF line into a license record.

    Expected format:
        185 AADIT BISWOKARMA 04-06-89128145 A CHABAHIL 2026-FEB-06

    Fields extracted:
        license_no, name, category, office, print_date
    """
    today = date.today().strftime("%Y-%m-%d")

    m = LICENSE_RE.search(line)
    if not m:
        return None

    license_no  = m.group(1)
    before      = line[:m.start()].strip()   # "185 AADIT BISWOKARMA"
    after       = line[m.end():].strip()     # "A CHABAHIL 2026-FEB-06"

    # ── Name: text before license_no, strip leading serial number ──────────
    name = re.sub(r'^\d+[\.\)\-\s]+', '', before).strip()
    name = re.sub(r'\s+', ' ', name).upper()
    if len(name) < 2:
        return None

    # ── Category: first token(s) after license_no matching letter codes ────
    category = ""
    cat_m = CAT_RE.match(after)          # must be at the START of `after`
    if cat_m:
        category = cat_m.group(0).upper().replace(' ', '')
        after = after[cat_m.end():].strip()

    # ── Date: last token in remaining text ─────────────────────────────────
    print_date = ""
    date_m = DATE_RE.search(after)
    if date_m:
        print_date = _normalize_date(date_m.group(1))
        after = after[:date_m.start()].strip()

    # ── Office: whatever remains between category and date ─────────────────
    # Remove any stray numbers/punctuation
    office = re.sub(r'\s+', ' ', after).strip()
    office = re.sub(r'^[\d\.\)\-]+', '', office).strip()

    return {
        "license_no":   license_no,
        "name":         name,
        "category":     category,
        "office":       office,
        "print_date":   print_date,
        "last_updated": today,
    }


def _parse_page_text(text: str) -> list[dict]:
    """Parse all license records from a single page's text."""
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        r = _parse_line(line)
        if r and len(r["license_no"]) >= 7:
            records.append(r)
    return records


# ── Worker function (runs in subprocess) ─────────────────────────────────────

def _extract_page_range(args):
    """Extract text from pages[start:end] of the PDF."""
    pdf_path, start, end = args
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            return []
    records = []
    try:
        reader = PdfReader(str(pdf_path))
        for i in range(start, min(end, len(reader.pages))):
            try:
                text = reader.pages[i].extract_text() or ""
                records.extend(_parse_page_text(text))
            except Exception:
                continue
    except Exception:
        pass
    return records


# ── Main entry point ──────────────────────────────────────────────────────────

def parse_and_store(pdf_path=None, workers: int = 4) -> int:
    """
    Parse the DOTM PDF and store all records into the database.
    Uses parallel processing for speed on large PDFs.
    """
    import db
    db.init_db()

    pdf = Path(pdf_path) if pdf_path else PDF_PATH
    if not pdf.exists():
        raise FileNotFoundError(f"PDF not found: {pdf}")

    log.info("Parsing PDF: %s", pdf)
    t_start = time.time()

    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader

    reader     = PdfReader(str(pdf))
    total_pages = len(reader.pages)
    log.info("Total pages: %d", total_pages)
    del reader

    BATCH   = 100
    batches = [
        (str(pdf), i, min(i + BATCH, total_pages))
        for i in range(0, total_pages, BATCH)
    ]

    all_records = []
    completed   = 0

    try:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_extract_page_range, b): b for b in batches}
            for future in as_completed(futures):
                try:
                    all_records.extend(future.result(timeout=60))
                except Exception as e:
                    log.warning("Batch failed: %s", e)
                finally:
                    completed += 1
                    pct       = completed * 100 // len(batches)
                    pages_done = min(completed * BATCH, total_pages)
                    print(
                        f"\r  Progress: {pages_done}/{total_pages} pages "
                        f"({pct}%)  |  Records: {len(all_records)}   ",
                        end="", flush=True
                    )
    except Exception:
        # Fallback: single-threaded (safer on some Windows setups)
        log.warning("Parallel failed, running single-threaded…")
        all_records = []
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        reader = PdfReader(str(pdf))
        for i, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
                all_records.extend(_parse_page_text(text))
            except Exception:
                continue
            if i % 100 == 0:
                print(
                    f"\r  Progress: {i}/{total_pages} pages "
                    f"({i*100//total_pages}%)  |  Records: {len(all_records)}   ",
                    end="", flush=True
                )

    print()

    # Deduplicate by license_no (keep last seen)
    dedup: dict[str, dict] = {}
    for r in all_records:
        key = r.get("license_no", "").upper()
        if key:
            dedup[key] = r

    clean = list(dedup.values())
    log.info("Unique records after dedup: %d", len(clean))

    if not clean:
        log.warning("No records extracted — check PDF format!")
        return 0

    count = db.upsert_licenses(clean)
    db.set_meta("last_updated", date.today().strftime("%Y-%m-%d"))

    elapsed = time.time() - t_start
    log.info("✓ Done in %.1fs — %d records imported", elapsed, count)
    return count


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Fast DOTM PDF Parser")
    ap.add_argument("--pdf",     default=str(PDF_PATH), help="Path to PDF")
    ap.add_argument("--workers", type=int, default=4,   help="Parallel workers (default 4)")
    args = ap.parse_args()

    try:
        n = parse_and_store(args.pdf, workers=args.workers)
        print(f"\n✓ Successfully imported {n} license records.")
        sys.exit(0)
    except FileNotFoundError as e:
        print(f"\n✗ {e}")
        sys.exit(1)
    except Exception as e:
        log.exception("Unexpected error")
        print(f"\n✗ Error: {e}")
        sys.exit(2)