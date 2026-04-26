"""
main.py  –  DOTM Nepal License Print Status Checker
Flask web server · REST API · Production-ready

Run:
    python main.py

Endpoints:
    GET  /                    → Serve index.html
    GET  /api/check?license=X → Check license print status
    GET  /api/stats           → Database stats (total records, last updated)
    POST /api/refresh         → Re-fetch + re-parse latest DOTM PDF (admin)
"""

import os
import sys
import time
import logging
from datetime import datetime

from flask import Flask, request, jsonify, render_template, abort
from flask_cors import CORS

import db

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── App setup ──────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
    static_url_path="/static",
)

CORS(app)   # Allow cross-origin requests (useful for separate front-end dev)

# ─── Startup ────────────────────────────────────────────────────────────────
@app.before_request
def _ensure_db():
    """Create tables if they don't exist yet (first-run safety)."""
    db.init_db()


# ─── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the main UI."""
    return render_template("index.html")


@app.route("/api/check")
def api_check():
    """
    Check print status for a given license number.

    Query params:
        license (str)  – The license number to look up.

    Returns JSON:
        {
            "found": true,
            "license_no": "07-01-00012345",
            "name": "RAM BAHADUR THAPA",
            "category": "B",
            "office": "Bagmati Yatayat Sewi Karyalaya",
            "print_date": "2081-05-15",
            "district": "Kathmandu",
            "last_updated": "2081-05-20"
        }

        or  {"found": false}
    """
    raw = request.args.get("license", "").strip()
    if not raw:
        return jsonify({"error": "License number is required"}), 400

    # Normalize: uppercase, remove extra spaces
    license_no = raw.upper().replace(" ", "")

    t0 = time.perf_counter()
    record = db.find_license(license_no)
    elapsed_ms = (time.perf_counter() - t0) * 1000

    log.info("LOOKUP  %-24s  found=%-5s  %.1fms", license_no, bool(record), elapsed_ms)

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
            "query_ms":     round(elapsed_ms, 2),
        })
    else:
        return jsonify({"found": False, "query_ms": round(elapsed_ms, 2)})


@app.route("/api/stats")
def api_stats():
    """
    Return database statistics for the stats strip on the homepage.
    """
    stats = db.get_stats()
    return jsonify(stats)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """
    Admin endpoint: re-download the latest DOTM PDF and re-parse it.
    Protect this with a simple secret key in production!
    """
    secret = request.headers.get("X-Admin-Key", "")
    if secret != os.environ.get("ADMIN_KEY", "dotm-admin-2081"):
        abort(403, "Forbidden")

    log.info("Manual refresh triggered via API")

    try:
        import fetch_pdf
        import parser

        pdf_path = fetch_pdf.download_latest_pdf()
        if not pdf_path:
            return jsonify({"ok": False, "error": "PDF download failed"}), 502

        count = parser.parse_and_store(pdf_path)
        log.info("Refresh complete – %d records imported", count)
        return jsonify({"ok": True, "records_imported": count})

    except Exception as exc:
        log.exception("Refresh failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


# ─── Health check ────────────────────────────────────────────────────────────

@app.route("/healthz")
def health():
    return jsonify({"status": "ok", "ts": datetime.utcnow().isoformat() + "Z"})


# ─── Error handlers ──────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error"}), 500


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()

    # First-run: load demo/sample data if the DB is empty
    if db.get_stats().get("total_records", 0) == 0:
        log.info("Empty database – loading sample data for demo…")
        db.load_sample_data()

    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "0") == "1"

    log.info("Starting DOTM License Status Server on http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=debug)