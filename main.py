"""
main.py  –  DOTM Nepal License Status Checker + Admin Panel
─────────────────────────────────────────────────────────────
Key fix: admin.html is rendered via render_template() with
admin_path=AP injected, so all JS fetch() calls use the real
secret slug instead of the hardcoded (decoy) /admin prefix.
"""

import os
import io
import csv
import json
import logging
import sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask, request, jsonify,
    render_template, redirect, url_for,
    session, Response, abort
)
from flask_cors import CORS

import db

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME",    "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD",    "dotm2081")
SECRET_KEY = os.environ.get("SECRET_KEY",        "dotm-secret-change-me-2081")

# ⚠️  Change this to something only you know.
# Set via env var:  export ADMIN_SECRET_PATH="your-secret-slug"
ADMIN_SECRET_PATH = os.environ.get("ADMIN_SECRET_PATH", "dotm-panel-x7k2")
AP = ADMIN_SECRET_PATH   # short alias


def _check_ap(_ap):
    """Return 404 if the path slug does not match the configured secret."""
    if _ap != AP:
        abort(404)


# PDF sources stored as JSON file
PDF_SOURCES_FILE = BASE_DIR / "pdf_sources.json"
CACHE_DIR = BASE_DIR / ".pdf_cache"

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=BASE_DIR / "templates",
    static_folder=BASE_DIR / "static",
    static_url_path="/static",
)
app.secret_key = SECRET_KEY
CORS(app)


# ── PDF Sources helpers ───────────────────────────────────────────────────────

def load_pdf_sources() -> dict:
    """
    Load PDF sources from JSON file.
    Each entry: { "OfficeName": { "url": "...", "district": "..." } }
    Legacy plain-string entries are normalised on first read.
    """
    if PDF_SOURCES_FILE.exists():
        try:
            raw = json.loads(PDF_SOURCES_FILE.read_text())
            normalised = {}
            for name, val in raw.items():
                if isinstance(val, str):
                    normalised[name] = {"url": val, "district": ""}
                else:
                    normalised[name] = val
            return normalised
        except Exception:
            pass

    # Default sources
    return {
        "Chabahil": {
            "url":      "https://giwmscdnone.gov.np/media/pdf_upload/Chabahil%20Printed%20License%20Card%20List_5siwppy.pdf",
            "district": "Kathmandu",
        },
        "Radheradhe": {
            "url":      "https://giwmscdnone.gov.np/media/pdf_upload/Radhe%20radhe%20Printed%20License%20175598_kmfke54.pdf",
            "district": "Bhaktapur",
        },
    }


def save_pdf_sources(sources: dict):
    PDF_SOURCES_FILE.write_text(json.dumps(
        sources, indent=2, ensure_ascii=False))


def _cache_path_for(office: str, url: str) -> Path:
    import hashlib
    CACHE_DIR.mkdir(exist_ok=True)
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    return CACHE_DIR / f"{office.lower().replace(' ', '_')}_{url_hash}.pdf"


# ── Auth decorator ────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(f"/{AP}/login")
        return f(*args, **kwargs)
    return decorated


# ── Startup ───────────────────────────────────────────────────────────────────

@app.before_request
def _ensure_db():
    db.init_db()


# ═══════════════════════════════════════════════════════════════════
#   PUBLIC ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/check")
def api_check():
    raw = request.args.get("license", "").strip()
    if not raw:
        return jsonify({"error": "License number required"}), 400

    license_no = raw.upper().replace(" ", "")
    record = db.find_license(license_no)

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
    return jsonify(db.get_stats())


@app.route("/healthz")
def health():
    return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat() + "Z"})


# ═══════════════════════════════════════════════════════════════════
#   SECURITY — DECOY: /admin always returns 404
# ═══════════════════════════════════════════════════════════════════

@app.route("/admin", defaults={"subpath": ""})
@app.route("/admin/<path:subpath>")
def admin_decoy(subpath):
    abort(404)


# ═══════════════════════════════════════════════════════════════════
#   ADMIN AUTH
# ═══════════════════════════════════════════════════════════════════

@app.route("/<string:_ap>/login", methods=["GET"])
def admin_login(_ap=None):
    _check_ap(_ap)
    if session.get("admin_logged_in"):
        return redirect(f"/{AP}")
    return render_template("admin_login.html")


@app.route("/<string:_ap>/login", methods=["POST"])
def admin_login_post(_ap=None):
    _check_ap(_ap)
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session["admin_logged_in"] = True
        session.permanent = True
        log.info("Admin login: %s", username)
        return redirect(f"/{AP}")

    log.warning("Failed admin login attempt for: %s", username)
    return redirect(f"/{AP}/login?error=1")


@app.route("/<string:_ap>/logout", methods=["POST"])
def admin_logout(_ap=None):
    _check_ap(_ap)
    session.clear()
    return redirect(f"/{AP}/login")


# ═══════════════════════════════════════════════════════════════════
#   ADMIN DASHBOARD
#   *** KEY FIX: pass admin_path=AP to the template so JS uses
#       the correct secret slug for all fetch() calls ***
# ═══════════════════════════════════════════════════════════════════

@app.route("/<string:_ap>")
@admin_required
def admin_dashboard(_ap=None):
    _check_ap(_ap)
    return render_template("admin.html", admin_path=AP)


# ═══════════════════════════════════════════════════════════════════
#   ADMIN API — PDF SOURCES
# ═══════════════════════════════════════════════════════════════════

@app.route("/<string:_ap>/api/urls", methods=["GET"])
@admin_required
def admin_get_urls(_ap=None):
    _check_ap(_ap)
    return jsonify({"urls": load_pdf_sources()})


@app.route("/<string:_ap>/api/urls", methods=["POST"])
@admin_required
def admin_add_url(_ap=None):
    _check_ap(_ap)
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
    log.info("Admin added PDF source: %s → %s (district: %s)",
             name, url[:60], district)
    return jsonify({"ok": True})


@app.route("/<string:_ap>/api/urls/<name>", methods=["DELETE"])
@admin_required
def admin_del_url(_ap=None, name=None):
    _check_ap(_ap)
    sources = load_pdf_sources()
    if name not in sources:
        return jsonify({"ok": False, "error": "Not found"}), 404

    entry = sources[name]
    url = entry["url"] if isinstance(entry, dict) else entry

    # 1. Delete cached PDF file if it exists
    cache_file = _cache_path_for(name, url)
    if cache_file.exists():
        cache_file.unlink()
        log.info("Deleted cached PDF: %s", cache_file.name)

    # 2. Delete all DB records for this office
    deleted_records = 0
    try:
        with db.get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM licenses WHERE office = ?", (name,))
            deleted_records = cur.rowcount
        log.info("Deleted %d DB records for office: %s", deleted_records, name)
    except Exception as e:
        log.error("Failed to delete DB records for %s: %s", name, e)

    # 3. Remove from sources config
    del sources[name]
    save_pdf_sources(sources)
    log.info("Admin removed PDF source: %s", name)

    return jsonify({"ok": True, "records_deleted": deleted_records})


# ═══════════════════════════════════════════════════════════════════
#   ADMIN API — SYNC
# ═══════════════════════════════════════════════════════════════════

@app.route("/<string:_ap>/api/sync", methods=["POST"])
@admin_required
def admin_sync(_ap=None):
    _check_ap(_ap)
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError
    import parser as pdf_parser

    CACHE_DIR.mkdir(exist_ok=True)

    HEADERS = {
        "User-Agent": "Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36",
        "Referer":    "https://dotm.gov.np/",
    }

    sources = load_pdf_sources()
    if not sources:
        return jsonify({"ok": False, "error": "No PDF sources configured. Add URLs first."}), 400

    downloaded = []
    for office, entry in sources.items():
        url = entry["url"] if isinstance(entry, dict) else entry
        district = entry.get("district", "") if isinstance(entry, dict) else ""

        local = _cache_path_for(office, url)

        try:
            log.info("Downloading [%s]: %s", office, url[:60])
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=60) as resp:
                data = resp.read()

            if len(data) > 512 and data.startswith(b"%PDF"):
                local.write_bytes(data)
                downloaded.append((office, local, district))
                log.info("Saved: %s (%d KB)", local.name, len(data) // 1024)
            else:
                log.warning("Invalid PDF for %s", office)
        except (HTTPError, URLError, OSError) as e:
            log.warning("Download failed [%s]: %s", office, e)

    if not downloaded:
        return jsonify({"ok": False, "error": "All downloads failed. Check PDF URLs."}), 502

    total = 0
    for office, pdf_path, district in downloaded:
        try:
            n = pdf_parser.parse_and_store(pdf_path, district=district)
            total += n
            log.info("[%s] %d records imported", office, n)
        except Exception as e:
            log.error("Parse failed [%s]: %s", office, e)

    return jsonify({
        "ok":               True,
        "pdfs_downloaded":  len(downloaded),
        "records_imported": total,
    })


# ═══════════════════════════════════════════════════════════════════
#   ADMIN API — SEARCH RECORDS
#   FIX: removed .upper() on name search — it broke mixed-case names
# ═══════════════════════════════════════════════════════════════════

@app.route("/<string:_ap>/api/search")
@admin_required
def admin_search(_ap=None):
    _check_ap(_ap)
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})

    pattern = f"%{q}%"
    try:
        with db.get_conn() as conn:
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


# ═══════════════════════════════════════════════════════════════════
#   ADMIN API — OFFICE BREAKDOWN
# ═══════════════════════════════════════════════════════════════════

@app.route("/<string:_ap>/api/breakdown")
@admin_required
def admin_breakdown(_ap=None):
    _check_ap(_ap)
    try:
        with db.get_conn() as conn:
            rows = conn.execute("""
                SELECT office, COUNT(*) as count
                FROM licenses
                GROUP BY office
                ORDER BY count DESC
            """).fetchall()
        return jsonify({"rows": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"rows": [], "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════
#   ADMIN API — EXPORT CSV
# ═══════════════════════════════════════════════════════════════════

@app.route("/<string:_ap>/api/export")
@admin_required
def admin_export(_ap=None):
    _check_ap(_ap)

    def generate():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["license_no", "name", "category",
                         "office", "print_date", "district", "last_updated"])
        yield output.getvalue()
        output.truncate(0)
        output.seek(0)

        conn = sqlite3.connect(db.DB_PATH)
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
    return Response(
        generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ═══════════════════════════════════════════════════════════════════
#   ADMIN API — CLEAR DB
# ═══════════════════════════════════════════════════════════════════

@app.route("/<string:_ap>/api/clear", methods=["POST"])
@admin_required
def admin_clear(_ap=None):
    _check_ap(_ap)
    try:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM licenses")
            conn.execute("DELETE FROM meta")

        deleted_files = 0
        if CACHE_DIR.exists():
            for f in CACHE_DIR.glob("*.pdf"):
                try:
                    f.unlink()
                    deleted_files += 1
                except OSError:
                    pass

        log.warning(
            "Admin cleared entire database + %d cached PDFs", deleted_files)
        return jsonify({"ok": True, "cached_pdfs_deleted": deleted_files})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════
#   ERROR HANDLERS
# ═══════════════════════════════════════════════════════════════════

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Server error"}), 500


# ═══════════════════════════════════════════════════════════════════
#   ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    db.init_db()

    if db.get_stats().get("total_records", 0) == 0:
        log.info("Empty DB — loading sample data…")
        db.load_sample_data()

    port = int(os.environ.get("PORT",  5000))
    debug = os.environ.get("DEBUG", "0") == "1"

    log.info("Server starting on http://0.0.0.0:%d", port)
    log.info("Admin panel → http://localhost:%d/%s", port, AP)
    log.info("Admin credentials → %s / %s", ADMIN_USERNAME, ADMIN_PASSWORD)
    app.run(host="0.0.0.0", port=port, debug=debug)
