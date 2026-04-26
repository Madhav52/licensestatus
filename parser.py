"""
parser.py  –  FAST DOTM Nepal License PDF Parser
──────────────────────────────────────────────────
Uses pypdf (fast text extraction) instead of pdfplumber.
Processes pages in parallel batches for maximum speed.

For 2549 pages: ~1-3 minutes instead of hanging.

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
from datetime import date
from concurrent.futures import ProcessPoolExecutor, as_completed

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
PDF_PATH = BASE_DIR / "license.pdf"

# License number pattern: XX-XX-XXXXXXXX
LICENSE_RE = re.compile(r'\b(\d{1,2}-\d{2,3}-\d{5,10})\b')
DATE_RE    = re.compile(r'\b(\d{4}[-/]\d{1,2}[-/]\d{1,2})\b')

# ─── Extract text from a page range (runs in subprocess) ─────────────────────

def _extract_page_range(args):
    """Worker function: extract text from pages[start:end] of the PDF."""
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
    except Exception as e:
        pass
    return records


def _parse_page_text(text: str) -> list[dict]:
    """Parse all license records from a single page's text."""
    records = []
    today = date.today().strftime("%Y-%m-%d")

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    for line in lines:
        m = LICENSE_RE.search(line)
        if not m:
            continue

        license_no = m.group(1)

        # Extract date from line
        print_date = ""
        dm = DATE_RE.search(line)
        if dm:
            print_date = dm.group(1).replace("/", "-")

        # Name: text before the license number, strip leading serial number
        before = line[:m.start()].strip()
        name = re.sub(r'^\d+[\.\)\s]+', '', before).strip()
        name = re.sub(r'\s+', ' ', name).upper()

        # Category: look for standalone letter codes after license number
        after = line[m.end():].strip()
        cat_match = re.search(r'\b([A-GKa-gk](?:\s*[,/]\s*[A-GKa-gk])*)\b', after[:30])
        category = cat_match.group(0).upper() if cat_match else ""

        if len(license_no) >= 7 and len(name) >= 2:
            records.append({
                "license_no":   license_no,
                "name":         name if name else "UNKNOWN",
                "category":     category,
                "office":       "",
                "print_date":   print_date,
                "district":     "",
                "last_updated": today,
            })

    return records


# ─── Try pdfplumber first (better table parsing) ──────────────────────────────

def _try_pdfplumber_fast(pdf_path: Path, max_pages: int = 50) -> list[dict] | None:
    """
    Quick test: try pdfplumber on first few pages.
    If it takes >30s for 50 pages, we skip it.
    """
    try:
        import pdfplumber
        import signal

        records = []
        start = time.time()

        with pdfplumber.open(str(pdf_path)) as pdf:
            test_pages = min(max_pages, len(pdf.pages))
            for i in range(test_pages):
                if time.time() - start > 20:  # 20s timeout for test
                    log.warning("pdfplumber too slow, switching to pypdf")
                    return None

                page = pdf.pages[i]
                # Try table extraction
                tables = page.extract_tables()
                if tables:
                    for table in tables:
                        for row in table:
                            cells = [str(c or "").strip() for c in row]
                            full_text = " ".join(cells)
                            m = LICENSE_RE.search(full_text)
                            if m:
                                license_no = m.group(1)
                                name = ""
                                for c in cells:
                                    if (len(c) > 3 and
                                        not LICENSE_RE.search(c) and
                                        not DATE_RE.search(c) and
                                        not c.isdigit() and
                                        re.match(r'^[A-Za-z\s\.]+$', c)):
                                        name = c.upper().strip()
                                        break
                                dm = DATE_RE.search(full_text)
                                records.append({
                                    "license_no":   license_no,
                                    "name":         name or "UNKNOWN",
                                    "category":     "",
                                    "office":       "",
                                    "print_date":   dm.group(1) if dm else "",
                                    "district":     "",
                                    "last_updated": date.today().strftime("%Y-%m-%d"),
                                })
                else:
                    text = page.extract_text() or ""
                    records.extend(_parse_page_text(text))

        elapsed = time.time() - start
        rate = test_pages / elapsed if elapsed > 0 else 0
        log.info("pdfplumber test: %d pages in %.1fs (%.1f pages/sec)", test_pages, elapsed, rate)

        if rate < 2.0:
            log.warning("pdfplumber rate %.1f pages/sec is too slow, using pypdf", rate)
            return None

        return records  # Signal: pdfplumber is fast enough

    except ImportError:
        return None
    except Exception as e:
        log.warning("pdfplumber error: %s", e)
        return None


# ─── Main fast parser ─────────────────────────────────────────────────────────

def parse_and_store(pdf_path=None, workers: int = 4) -> int:
    """
    Fast parallel PDF parser.
    Uses pypdf with multiprocessing for speed.
    """
    import db

    # Always ensure tables exist before inserting
    db.init_db()

    pdf = Path(pdf_path) if pdf_path else PDF_PATH
    if not pdf.exists():
        raise FileNotFoundError(f"PDF not found: {pdf}")

    log.info("Parsing PDF: %s", pdf)
    t_start = time.time()

    # Check total pages quickly
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader

    reader = PdfReader(str(pdf))
    total_pages = len(reader.pages)
    log.info("Total pages: %d", total_pages)
    del reader  # Free memory

    # Quick pdfplumber test for small PDFs
    if total_pages <= 200:
        result = _try_pdfplumber_fast(pdf, max_pages=total_pages)
        if result is not None:
            log.info("Using pdfplumber: extracted %d records from test", len(result))
            # Run full pdfplumber parse
            try:
                import pdfplumber
                all_records = []
                with pdfplumber.open(str(pdf)) as p:
                    for page in p.pages:
                        tables = page.extract_tables()
                        if tables:
                            for table in tables:
                                for row in table:
                                    cells = [str(c or "").strip() for c in row]
                                    full_text = " ".join(cells)
                                    m = LICENSE_RE.search(full_text)
                                    if m:
                                        all_records.append({
                                            "license_no": m.group(1),
                                            "name": "UNKNOWN",
                                            "category": "",
                                            "office": "",
                                            "print_date": "",
                                            "district": "",
                                            "last_updated": date.today().strftime("%Y-%m-%d"),
                                        })
                        else:
                            all_records.extend(_parse_page_text(page.extract_text() or ""))
                clean = [r for r in all_records if r.get("license_no")]
                count = db.upsert_licenses(clean)
                db.set_meta("last_updated", date.today().strftime("%Y-%m-%d"))
                log.info("Done in %.1fs: %d records", time.time() - t_start, count)
                return count
            except Exception:
                pass  # Fall through to pypdf

    # ── Fast parallel pypdf extraction ──
    log.info("Using fast parallel pypdf extraction with %d workers", workers)

    BATCH = 100  # pages per worker batch
    batches = [
        (str(pdf), i, min(i + BATCH, total_pages))
        for i in range(0, total_pages, BATCH)
    ]

    all_records = []
    completed = 0

    # Use ProcessPoolExecutor for true parallelism
    try:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_extract_page_range, b): b for b in batches}
            for future in as_completed(futures):
                try:
                    result = future.result(timeout=60)
                    all_records.extend(result)
                except Exception as e:
                    log.warning("Batch failed: %s", e)
                finally:
                    completed += 1
                    pct = completed * 100 // len(batches)
                    pages_done = min(completed * BATCH, total_pages)
                    print(
                        f"\r  Progress: {pages_done}/{total_pages} pages "
                        f"({pct}%)  |  Records: {len(all_records)}   ",
                        end="", flush=True
                    )

    except Exception:
        # Fallback: single-threaded if multiprocessing fails (Windows sometimes)
        log.warning("Parallel processing failed, running single-threaded…")
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
                pct = i * 100 // total_pages
                print(f"\r  Progress: {i}/{total_pages} pages ({pct}%)  |  Records: {len(all_records)}   ",
                      end="", flush=True)

    print()  # newline after progress

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


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    ap = argparse.ArgumentParser(description="Fast DOTM PDF Parser")
    ap.add_argument("--pdf", default=str(PDF_PATH), help="Path to PDF")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel workers (default 4)")
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