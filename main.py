"""
main.py – DOTM Nepal License Status Checker (single-file edition)
─────────────────────────────────────────────────────────────────
All app logic lives here:
  • Flask routes (public + admin)
  • SQLite database layer
  • PDF parser
  • PDF source manager (downloads via admin-supplied URLs)
  • Keepalive (prevents Render free tier from sleeping)

Records originate exclusively from PDF URLs added by the admin —
no sample data is ever loaded.

PDFs are downloaded temporarily and deleted immediately after
records are stored in the database — no local PDF files are kept.
"""

import os
import io
import re
import csv
import json
import time
import hashlib
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from flask import (
    Flask, request, jsonify,
    render_template, redirect,
    session, Response, abort
)
from flask_cors import CORS


# ══════════════════════════════════════════════════════════════════
#   LOGGING
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
#   CONFIG
# ══════════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "licenses.db"
PDF_SOURCES_FILE = BASE_DIR / "pdf_sources.json"
TEMP_DIR = BASE_DIR / ".pdf_tmp"

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "dotm2081")
SECRET_KEY = os.environ.get("SECRET_KEY",     "dotm-secret-change-me-2081")


# ══════════════════════════════════════════════════════════════════
#   KEEPALIVE  (prevents Render free tier from sleeping)
# ══════════════════════════════════════════════════════════════════
def _keepalive_loop():
    """Ping own /healthz every 10 min so Render never spins down."""
    time.sleep(90)
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        log.info("Keepalive: RENDER_EXTERNAL_URL not set — skipping")
        return
    ping_url = url + "/healthz"
    log.info("Keepalive started → %s (every 10 min)", ping_url)
    while True:
        try:
            with urlopen(ping_url, timeout=10) as r:
                log.debug("Keepalive OK (%d)", r.status)
        except Exception as e:
            log.debug("Keepalive ping failed: %s", e)
        time.sleep(600)


def start_keepalive():
    """Start the background keepalive thread (only on Render)."""
    if not os.environ.get("RENDER"):
        return
    t = threading.Thread(target=_keepalive_loop, daemon=True)
    t.start()


# ══════════════════════════════════════════════════════════════════
#   DATABASE LAYER
# ══════════════════════════════════════════════════════════════════
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA temp_store=MEMORY")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS licenses (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                license_no   TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                name         TEXT    NOT NULL,
                category     TEXT    DEFAULT '',
                office       TEXT    DEFAULT '',
                print_date   TEXT    DEFAULT '',
                district     TEXT    DEFAULT '',
                last_updated TEXT    DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_license_no
                ON licenses (license_no COLLATE NOCASE);

            CREATE INDEX IF NOT EXISTS idx_office
                ON licenses (office);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)


def find_license(license_no: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE license_no = ? COLLATE NOCASE",
            (license_no.strip(),)
        ).fetchone()
    return dict(row) if row else None


def upsert_licenses(records: list[dict]) -> int:
    if not records:
        return 0

    sql = """
        INSERT INTO licenses
            (license_no, name, category, office, print_date, district, last_updated)
        VALUES
            (:license_no, :name, :category, :office, :print_date, :district, :last_updated)
        ON CONFLICT(license_no) DO UPDATE SET
            name         = excluded.name,
            category     = excluded.category,
            office       = excluded.office,
            print_date   = excluded.print_date,
            district     = excluded.district,
            last_updated = excluded.last_updated
    """

    today = date.today().strftime("%Y-%m-%d")
    for r in records:
        r.setdefault("last_updated", today)
        r.setdefault("district",     "")
        r.setdefault("category",     "")
        r.setdefault("office",       "")
        r.setdefault("print_date",   "")
        r.setdefault("name",         "UNKNOWN")

    with get_conn() as conn:
        conn.executemany(sql, records)

    log.info("Upserted %d license records", len(records))
    return len(records)


def get_stats() -> dict:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        meta = conn.execute(
            "SELECT value FROM meta WHERE key='last_updated'"
        ).fetchone()
    return {
        "total_records": total,
        "last_updated":  meta[0] if meta else "—",
    }


def get_office_breakdown() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT office,
                   COALESCE(NULLIF(district, ''), '') AS district,
                   COUNT(*) AS count
            FROM licenses
            WHERE office IS NOT NULL AND office != ''
            GROUP BY office
            ORDER BY count DESC
        """).fetchall()
    return [dict(r) for r in rows]


def set_meta(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )


# ══════════════════════════════════════════════════════════════════
#   PDF PARSER
# ══════════════════════════════════════════════════════════════════
LICENSE_RE = re.compile(r'\b(\d{1,2}-\d{2,3}-\d{5,10})\b')

DATE_RE = re.compile(
    r'\b(\d{4}[-/](?:\d{1,2}|JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[-/]\d{1,2})\b',
    re.IGNORECASE
)

CAT_RE = re.compile(r'\b([A-GKa-gk](?:\s*[,/]\s*[A-GKa-gk])*)\b')

MONTH_MAP = {
    'JAN': '01', 'FEB': '02', 'MAR': '03', 'APR': '04',
    'MAY': '05', 'JUN': '06', 'JUL': '07', 'AUG': '08',
    'SEP': '09', 'OCT': '10', 'NOV': '11', 'DEC': '12',
}


def _normalize_date(raw: str) -> str:
    raw = raw.strip()
    for mon, num in MONTH_MAP.items():
        raw = raw.upper().replace(
            f'-{mon}-', f'-{num}-').replace(f'/{mon}/', f'/{num}/')
    return raw.replace('/', '-')


def _parse_line(line: str):
    today = date.today().strftime("%Y-%m-%d")
    m = LICENSE_RE.search(line)
    if not m:
        return None

    license_no = m.group(1)
    before = line[:m.start()].strip()
    after = line[m.end():].strip()

    name = re.sub(r'^\d+[\.\)\-\s]+', '', before).strip()
    name = re.sub(r'\s+', ' ', name).upper()
    if len(name) < 2:
        return None

    category = ""
    cat_m = CAT_RE.match(after)
    if cat_m:
        category = cat_m.group(0).upper().replace(' ', '')
        after = after[cat_m.end():].strip()

    print_date = ""
    date_m = DATE_RE.search(after)
    if date_m:
        print_date = _normalize_date(date_m.group(1))
        after = after[:date_m.start()].strip()

    office = re.sub(r'\s+', ' ', after).strip()
    office = re.sub(r'^[\d\.\)\-]+', '', office).strip()

    return {
        "license_no": license_no,
        "name":       name,
        "category":   category,
        "office":     office,
        "print_date": print_date,
        "last_updated": today,
    }


def _parse_page_text(text: str) -> list[dict]:
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        r = _parse_line(line)
        if r and len(r["license_no"]) >= 7:
            records.append(r)
    return records


def parse_and_store(pdf_path, district: str = "", office_override: str = "") -> int:
    init_db()
    pdf = Path(pdf_path)
    if not pdf.exists():
        raise FileNotFoundError(f"PDF not found: {pdf}")

    log.info("Parsing PDF: %s", pdf)
    t_start = time.time()

    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader

    reader = PdfReader(str(pdf))
    total_pages = len(reader.pages)
    log.info("Total pages: %d", total_pages)

    all_records = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
            all_records.extend(_parse_page_text(text))
        except Exception as e:
            log.warning("Page %d failed: %s", i, e)
            continue

    dedup: dict[str, dict] = {}
    for r in all_records:
        key = r.get("license_no", "").upper()
        if not key:
            continue
        if office_override:
            r["office"] = office_override
        if district:
            r["district"] = district
        dedup[key] = r

    clean = list(dedup.values())
    log.info("Unique records after dedup: %d", len(clean))

    if not clean:
        log.warning("No records extracted — check PDF format!")
        return 0

    count = upsert_licenses(clean)
    set_meta("last_updated", date.today().strftime("%Y-%m-%d"))
    log.info("Done in %.1fs — %d records imported",
             time.time() - t_start, count)
    return count


# ══════════════════════════════════════════════════════════════════
#   PDF SOURCES
# ══════════════════════════════════════════════════════════════════
def load_pdf_sources() -> dict:
    if PDF_SOURCES_FILE.exists():
        try:
            raw = json.loads(PDF_SOURCES_FILE.read_text(encoding="utf-8"))
            normalised = {}
            for name, val in raw.items():
                if isinstance(val, str):
                    normalised[name] = {"url": val, "district": ""}
                else:
                    normalised[name] = val
            return normalised
        except Exception:
            pass
    return {}


def save_pdf_sources(sources: dict):
    PDF_SOURCES_FILE.write_text(
        json.dumps(sources, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def _temp_pdf_path(office: str) -> Path:
    TEMP_DIR.mkdir(exist_ok=True)
    safe = re.sub(r'[^\w]', '_', office.lower())
    return TEMP_DIR / f"{safe}.pdf"


def _delete_temp_pdf(path: Path):
    try:
        if path.exists():
            path.unlink()
            log.info("Deleted temp PDF: %s", path.name)
    except OSError as e:
        log.warning("Could not delete temp PDF %s: %s", path.name, e)


# ══════════════════════════════════════════════════════════════════
#   AUTH
# ══════════════════════════════════════════════════════════════════
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect("/dotm-admin/login")
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════════
#   FLASK APP
# ══════════════════════════════════════════════════════════════════
app = Flask(
    __name__,
    template_folder=BASE_DIR / "templates",
    static_folder=BASE_DIR / "static",
    static_url_path="/static",
)
app.secret_key = SECRET_KEY
CORS(app)


@app.before_request
def _ensure_db():
    init_db()


# ══════════════════════════════════════════════════════════════════
#   PWA — Service Worker & Manifest
#   These MUST be served from root (/) — not /static/ — for PWA
#   to work correctly in all browsers.
# ══════════════════════════════════════════════════════════════════
@app.route("/sw.js")
def service_worker():
    """Serve the service worker from root with no-cache headers."""
    response = app.send_static_file("sw.js")
    response.headers["Content-Type"] = "application/javascript"
    # SW must never be cached — browser needs the latest version always
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/manifest.json")
def manifest():
    """Serve the PWA manifest from root."""
    response = app.send_static_file("manifest.json")
    response.headers["Content-Type"] = "application/manifest+json"
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


# ══════════════════════════════════════════════════════════════════
#   PUBLIC ROUTES
# ══════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/check")
def api_check():
    raw = request.args.get("license", "").strip()
    if not raw:
        return jsonify({"error": "License number required"}), 400
    license_no = raw.upper().replace(" ", "")
    record = find_license(license_no)
    if record:
        return jsonify({
            "found":        True,
            "license_no":   record.get("license_no"),
            "name":         record.get("name"),
            "category":     record.get("category"),
            "office":       record.get("office"),
            "print_date":   record.get("print_date"),
            "district":     record.get("district"),
            "last_updated": record.get("last_updated"),
        })
    return jsonify({"found": False})


@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


@app.route("/api/offices")
def api_offices():
    rows = get_office_breakdown()
    total = sum(r["count"] for r in rows)
    return jsonify({"offices": rows, "total": total})


@app.route("/healthz")
def health():
    return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat() + "Z"})


# ══════════════════════════════════════════════════════════════════
#   DECOY — /admin always 404 so public never finds real panel
# ══════════════════════════════════════════════════════════════════
@app.route("/admin", defaults={"subpath": ""})
@app.route("/admin/<path:subpath>")
def admin_decoy(subpath):
    abort(404)


# ══════════════════════════════════════════════════════════════════
#   ADMIN AUTH
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/login", methods=["GET"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect("/dotm-admin")
    return render_template("admin_login.html")


@app.route("/dotm-admin/login", methods=["POST"])
def admin_login_post():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session["admin_logged_in"] = True
        session.permanent = True
        log.info("Admin login: %s", username)
        return redirect("/dotm-admin")
    log.warning("Failed admin login: %s", username)
    return redirect("/dotm-admin/login?error=1")


@app.route("/dotm-admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect("/dotm-admin/login")


# ══════════════════════════════════════════════════════════════════
#   ADMIN DASHBOARD
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin")
@admin_required
def admin_dashboard():
    return render_template("admin.html")


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — PDF SOURCES
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/urls", methods=["GET"])
@admin_required
def admin_get_urls():
    return jsonify({"urls": load_pdf_sources()})


@app.route("/dotm-admin/api/urls", methods=["POST"])
@admin_required
def admin_add_url():
    data = request.get_json() or {}
    name = data.get("name",     "").strip()
    url = data.get("url",      "").strip()
    district = data.get("district", "").strip()
    if not name or not url:
        return jsonify({"ok": False, "error": "Name and URL required"}), 400
    if not url.startswith("http"):
        return jsonify({"ok": False, "error": "Invalid URL"}), 400
    sources = load_pdf_sources()
    sources[name] = {"url": url, "district": district}
    save_pdf_sources(sources)
    log.info("Added source: %s → %s (district: %s)", name, url[:60], district)
    return jsonify({"ok": True})


@app.route("/dotm-admin/api/urls/<name>", methods=["DELETE"])
@admin_required
def admin_del_url(name=None):
    sources = load_pdf_sources()
    if name not in sources:
        return jsonify({"ok": False, "error": "Not found"}), 404
    _delete_temp_pdf(_temp_pdf_path(name))
    deleted_records = 0
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM licenses WHERE office = ?", (name,))
            deleted_records = cur.rowcount
    except Exception as e:
        log.error("Failed to delete DB records for %s: %s", name, e)
    del sources[name]
    save_pdf_sources(sources)
    log.info("Removed source: %s (%d records deleted)", name, deleted_records)
    return jsonify({"ok": True, "records_deleted": deleted_records})


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — SYNC
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/sync", methods=["POST"])
@admin_required
def admin_sync():
    TEMP_DIR.mkdir(exist_ok=True)
    headers = {
        "User-Agent": "Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36",
        "Referer":    "https://dotm.gov.np/",
    }
    sources = load_pdf_sources()
    if not sources:
        return jsonify({"ok": False, "error": "No PDF sources configured."}), 400

    results = []
    total_imported = 0

    for office, entry in sources.items():
        url = entry["url"] if isinstance(entry, dict) else entry
        district = entry.get("district", "") if isinstance(entry, dict) else ""
        tmp_pdf = _temp_pdf_path(office)

        try:
            with get_conn() as conn:
                existing = conn.execute(
                    "SELECT COUNT(*) FROM licenses WHERE office = ?", (office,)
                ).fetchone()[0]
        except Exception:
            existing = 0

        if existing > 0:
            log.info(
                "Skipping [%s] — %d records already in DB", office, existing)
            results.append({
                "office": office, "status": "skipped",
                "records": existing,
                "message": f"{existing} records already loaded"
            })
            continue

        try:
            log.info("Downloading [%s]: %s", office, url[:80])
            req = Request(url, headers=headers)
            with urlopen(req, timeout=60) as resp:
                data = resp.read()
            if not (len(data) > 512 and data.startswith(b"%PDF")):
                results.append({
                    "office": office, "status": "invalid_pdf",
                    "records": 0, "message": "Response was not a valid PDF"
                })
                continue
            tmp_pdf.write_bytes(data)
        except (HTTPError, URLError, OSError) as e:
            results.append({
                "office": office, "status": "download_failed",
                "records": 0, "message": str(e)
            })
            continue

        try:
            n = parse_and_store(tmp_pdf, district=district,
                                office_override=office)
            total_imported += n
            results.append({
                "office": office, "status": "imported",
                "records": n, "message": f"{n} records imported"
            })
        except Exception as e:
            log.error("Parse failed [%s]: %s", office, e)
            results.append({
                "office": office, "status": "parse_failed",
                "records": 0, "message": str(e)
            })
        finally:
            _delete_temp_pdf(tmp_pdf)

    imported = [r for r in results if r["status"] == "imported"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] not in ("imported", "skipped")]

    return jsonify({
        "ok":               True,
        "pdfs_downloaded":  len(imported),
        "pdfs_skipped":     len(skipped),
        "pdfs_failed":      len(failed),
        "records_imported": total_imported,
        "details":          results,
    })


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — SEARCH
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/search")
@admin_required
def admin_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    pattern = f"%{q}%"
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT license_no, name, category, office, print_date, district
                FROM licenses
                WHERE license_no LIKE ?
                   OR name       LIKE ?
                ORDER BY name
                LIMIT 50
            """, (pattern, pattern)).fetchall()
        return jsonify({"results": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — BREAKDOWN
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/breakdown")
@admin_required
def admin_breakdown():
    try:
        return jsonify({"rows": get_office_breakdown()})
    except Exception as e:
        return jsonify({"rows": [], "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — EXPORT CSV
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/export")
@admin_required
def admin_export():
    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["license_no", "name", "category",
                         "office", "print_date", "district", "last_updated"])
        yield output.getvalue()
        output.truncate(0)
        output.seek(0)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                "SELECT license_no,name,category,office,print_date,district,last_updated "
                "FROM licenses ORDER BY license_no"
            ):
                writer.writerow([
                    row["license_no"], row["name"],       row["category"],
                    row["office"],     row["print_date"], row["district"],
                    row["last_updated"]
                ])
                yield output.getvalue()
                output.truncate(0)
                output.seek(0)
        finally:
            conn.close()

    filename = f"dotm_licenses_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(generate(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# ══════════════════════════════════════════════════════════════════
#   ADMIN API — CLEAR DB
# ══════════════════════════════════════════════════════════════════
@app.route("/dotm-admin/api/clear", methods=["POST"])
@admin_required
def admin_clear():
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM licenses")
            conn.execute("DELETE FROM meta")
        deleted_files = 0
        if TEMP_DIR.exists():
            for f in TEMP_DIR.glob("*.pdf"):
                try:
                    f.unlink()
                    deleted_files += 1
                except OSError:
                    pass
        log.warning("Admin cleared DB + %d temp PDFs", deleted_files)
        return jsonify({"ok": True, "temp_pdfs_deleted": deleted_files})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
#   ERROR HANDLERS
# ══════════════════════════════════════════════════════════════════
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Server error"}), 500


# ══════════════════════════════════════════════════════════════════
#   ENTRY POINT
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    init_db()
    start_keepalive()

    port = int(os.environ.get("PORT",  5000))
    debug = os.environ.get("DEBUG", "0") == "1"

    log.info("Server  →  http://0.0.0.0:%d", port)
    log.info("Admin   →  http://localhost:%d/dotm-admin", port)
    log.info("Login   →  %s / %s", ADMIN_USERNAME, ADMIN_PASSWORD)
    app.run(host="0.0.0.0", port=port, debug=debug)
