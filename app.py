"""
Tabla Compositions DB — Flask + SQLite backend.

Single-file app. Run with:  python3 app.py
Then open http://localhost:5000 in a browser.
"""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, request, send_from_directory, send_file, Response

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "compositions.db"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Port for the dev server. macOS AirPlay Receiver sits on 5000 by default,
# so we use 5050 unless PORT is set in the environment.
PORT = int(os.environ.get("PORT", "5050"))

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "application/pdf"}

# Tiny starter set — just enough so the matcher isn't blank on day one.
# The user adds their own bols from the UI; the dictionary grows with them.
SEED_BOLS = ["Dha", "Ghe", "Dhin", "Tin", "Tun", "Dhun", "Ne", "Ti"]

# Seed composition types inserted on first run if the table is empty.
# Start with only Kaida — user will add Tukda/Rela/etc. from the UI as needed.
SEED_TYPES = [
    ("kaida", "Kaida"),
]

# Compulsory fields — empty values rejected.
COMPULSORY = ("name", "taal", "speed_group")

# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #


def get_db() -> sqlite3.Connection:
    """Open a connection with row-dict access and foreign keys enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    """Create tables and seed defaults if empty. Idempotent — safe to call on every boot."""
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS composition_types (
            name        TEXT PRIMARY KEY,
            label       TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS compositions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            type          TEXT NOT NULL,
            name          TEXT NOT NULL,
            taal          TEXT NOT NULL,
            speed_group   TEXT NOT NULL,
            bol_type      TEXT,
            gharana       TEXT,
            miscell_info  TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            FOREIGN KEY(type) REFERENCES composition_types(name) ON UPDATE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_compositions_type ON compositions(type);
        CREATE INDEX IF NOT EXISTS idx_compositions_taal ON compositions(taal);

        CREATE TABLE IF NOT EXISTS attachments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            composition_id INTEGER NOT NULL,
            filename      TEXT NOT NULL,
            mime          TEXT NOT NULL,
            size_bytes    INTEGER NOT NULL,
            created_at    TEXT NOT NULL,
            FOREIGN KEY(composition_id) REFERENCES compositions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_attachments_comp ON attachments(composition_id);

        CREATE TABLE IF NOT EXISTS bols (
            name        TEXT PRIMARY KEY,   -- canonical bol, e.g. "Dha"
            created_at  TEXT NOT NULL
        );
    """
    )
    # Seed composition types if none exist.
    count = cur.execute("SELECT COUNT(*) FROM composition_types").fetchone()[0]
    if count == 0:
        now = now_iso()
        cur.executemany(
            "INSERT INTO composition_types (name, label, created_at) VALUES (?, ?, ?)",
            [(name, label, now) for name, label in SEED_TYPES],
        )
    # Seed starter bols if none exist. User-added bols are preserved across restarts.
    bol_count = cur.execute("SELECT COUNT(*) FROM bols").fetchone()[0]
    if bol_count == 0:
        now = now_iso()
        cur.executemany(
            "INSERT INTO bols (name, created_at) VALUES (?, ?)",
            [(b, now) for b in SEED_BOLS],
        )
    conn.commit()
    conn.close()


def now_iso() -> str:
    """ISO 8601 UTC, second precision — good enough and stable for sort order."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def require_json() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, "Expected a JSON object body")
    return data


def validate_compulsory(data: dict[str, Any]) -> None:
    missing = [f for f in COMPULSORY if not str(data.get(f, "")).strip()]
    if missing:
        abort(400, f"Compulsory fields cannot be empty: {', '.join(missing)}")


def validate_type_exists(conn: sqlite3.Connection, type_name: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM composition_types WHERE name = ?", (type_name,)
    ).fetchone()
    if row is None:
        abort(400, f"Unknown composition type: {type_name!r}")


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES  # request-body cap (covers all routes)


# ---- Static UI ---- #


@app.get("/")
def index():
    """Serve the single-page UI."""
    return send_file(BASE_DIR / "index.html")


# ---- Composition types ---- #


@app.get("/api/composition_types")
def list_types():
    conn = get_db()
    rows = conn.execute(
        "SELECT name, label, created_at FROM composition_types ORDER BY label"
    ).fetchall()
    conn.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.post("/api/composition_types")
def create_type():
    data = require_json()
    label = str(data.get("label", "")).strip()
    slug = str(data.get("name", "")).strip().lower()
    if not label:
        abort(400, "label is required")
    if not SLUG_RE.match(slug):
        abort(400, "name must be a URL-safe slug (a-z, 0-9, _, -)")
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO composition_types (name, label, created_at) VALUES (?, ?, ?)",
            (slug, label, now_iso()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        abort(409, f"Composition type {slug!r} already exists")
    finally:
        conn.close()
    return jsonify({"name": slug, "label": label}), 201


# ---- Bols (for dictation) ---- #


@app.get("/api/bols")
def list_bols():
    conn = get_db()
    rows = conn.execute("SELECT name FROM bols ORDER BY name").fetchall()
    conn.close()
    return jsonify([r["name"] for r in rows])


@app.post("/api/bols")
def add_bol():
    """Add a single bol to the dictionary. Idempotent — duplicate is a no-op."""
    data = require_json()
    name = str(data.get("name", "")).strip()
    if not name:
        abort(400, "name is required")
    if len(name) > 40:
        abort(400, "bol name too long (max 40 chars)")
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO bols (name, created_at) VALUES (?, ?)",
            (name, now_iso()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # already in the dictionary — fine
    finally:
        conn.close()
    return jsonify({"name": name}), 201


@app.delete("/api/bols/<name>")
def remove_bol(name: str):
    """Remove a bol from the dictionary."""
    conn = get_db()
    cur = conn.execute("DELETE FROM bols WHERE name = ?", (name,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    if not deleted:
        abort(404, f"Bol {name!r} not found")
    return ("", 204)


# ---- Compositions ---- #


def _parse_filters() -> dict[str, Any]:
    """Pull query-string filters into a normalized dict."""
    types = [t for t in request.args.get("type", "").split(",") if t]
    taal = request.args.get("taal", "").strip()
    speed_group = request.args.get("speed_group", "").strip()
    q = request.args.get("q", "").strip()
    return {"types": types, "taal": taal, "speed_group": speed_group, "q": q}


def _build_where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if filters["types"]:
        placeholders = ",".join("?" * len(filters["types"]))
        clauses.append(f"type IN ({placeholders})")
        params.extend(filters["types"])
    if filters["taal"]:
        clauses.append("taal = ?")
        params.append(filters["taal"])
    if filters["speed_group"]:
        clauses.append("speed_group = ?")
        params.append(filters["speed_group"])
    if filters["q"]:
        clauses.append("(name LIKE ? OR miscell_info LIKE ?)")
        like = f"%{filters['q']}%"
        params.extend([like, like])
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where, params


@app.get("/api/compositions")
def list_compositions():
    filters = _parse_filters()
    where, params = _build_where(filters)
    conn = get_db()
    rows = conn.execute(
        f"SELECT * FROM compositions {where} ORDER BY updated_at DESC, id DESC",
        params,
    ).fetchall()
    result = []
    for r in rows:
        d = row_to_dict(r)
        atts = conn.execute(
            "SELECT id, filename, mime, size_bytes, created_at FROM attachments "
            "WHERE composition_id = ? ORDER BY id",
            (d["id"],),
        ).fetchall()
        d["attachments"] = [row_to_dict(a) for a in atts]
        result.append(d)
    conn.close()
    return jsonify(result)


@app.post("/api/compositions")
def create_composition():
    data = require_json()
    validate_compulsory(data)
    type_name = str(data.get("type", "")).strip()
    if not type_name:
        abort(400, "type is required")
    conn = get_db()
    validate_type_exists(conn, type_name)
    now = now_iso()
    cur = conn.execute(
        """INSERT INTO compositions
           (type, name, taal, speed_group, bol_type, gharana, miscell_info, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            type_name,
            str(data["name"]).strip(),
            str(data["taal"]).strip(),
            str(data["speed_group"]).strip(),
            str(data.get("bol_type", "")).strip() or None,
            str(data.get("gharana", "")).strip() or None,
            str(data.get("miscell_info", "")).strip() or None,
            now,
            now,
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    row = conn.execute("SELECT * FROM compositions WHERE id = ?", (new_id,)).fetchone()
    out = row_to_dict(row)
    out["attachments"] = []
    conn.close()
    return jsonify(out), 201


@app.put("/api/compositions/<int:comp_id>")
def update_composition(comp_id: int):
    data = require_json()
    conn = get_db()
    row = conn.execute("SELECT * FROM compositions WHERE id = ?", (comp_id,)).fetchone()
    if row is None:
        conn.close()
        abort(404, "Composition not found")
    # Partial update — only fields present in the body are changed.
    editable = ("type", "name", "taal", "speed_group", "bol_type", "gharana", "miscell_info")
    updates: dict[str, Any] = {}
    for k in editable:
        if k in data:
            updates[k] = str(data[k]).strip() or None
    if "type" in updates and updates["type"]:
        validate_type_exists(conn, updates["type"])
    # Re-validate compulsory fields after merge.
    merged = {**row_to_dict(row), **{k: (v if v is not None else "") for k, v in updates.items()}}
    validate_compulsory(merged)
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [now_iso(), comp_id]
        conn.execute(f"UPDATE compositions SET {sets}, updated_at = ? WHERE id = ?", params)
        conn.commit()
    row = conn.execute("SELECT * FROM compositions WHERE id = ?", (comp_id,)).fetchone()
    atts = conn.execute(
        "SELECT id, filename, mime, size_bytes, created_at FROM attachments "
        "WHERE composition_id = ? ORDER BY id",
        (comp_id,),
    ).fetchall()
    out = row_to_dict(row)
    out["attachments"] = [row_to_dict(a) for a in atts]
    conn.close()
    return jsonify(out)


@app.delete("/api/compositions/<int:comp_id>")
def delete_composition(comp_id: int):
    conn = get_db()
    row = conn.execute("SELECT id FROM compositions WHERE id = ?", (comp_id,)).fetchone()
    if row is None:
        conn.close()
        abort(404, "Composition not found")
    # Best-effort: remove upload directory for this composition.
    conn.execute("DELETE FROM compositions WHERE id = ?", (comp_id,))
    conn.commit()
    conn.close()
    shutil.rmtree(UPLOAD_DIR / str(comp_id), ignore_errors=True)
    return ("", 204)


# ---- CSV export ---- #


@app.get("/api/export.csv")
def export_csv():
    filters = _parse_filters()
    where, params = _build_where(filters)
    conn = get_db()
    rows = conn.execute(
        f"SELECT * FROM compositions {where} ORDER BY type, name", params
    ).fetchall()
    conn.close()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["id", "type", "name", "taal", "speed_group", "bol_type", "gharana", "miscell_info", "updated_at"]
    )
    for r in rows:
        writer.writerow([r["id"], r["type"], r["name"], r["taal"], r["speed_group"],
                         r["bol_type"] or "", r["gharana"] or "", r["miscell_info"] or "", r["updated_at"]])
    data = buf.getvalue()
    return Response(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=compositions.csv"},
    )


# ---- Attachments ---- #


def _comp_dir(comp_id: int) -> Path:
    d = UPLOAD_DIR / str(comp_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.post("/api/compositions/<int:comp_id>/attachments")
def upload_attachment(comp_id: int):
    conn = get_db()
    row = conn.execute("SELECT id FROM compositions WHERE id = ?", (comp_id,)).fetchone()
    if row is None:
        conn.close()
        abort(404, "Composition not found")
    f = request.files.get("file")
    if f is None or not f.filename:
        conn.close()
        abort(400, "multipart field 'file' is required")
    mime = (f.mimetype or "").lower()
    if mime not in ALLOWED_MIME:
        conn.close()
        abort(400, f"Unsupported file type: {mime!r}. Allowed: {', '.join(sorted(ALLOWED_MIME))}")
    # Sanitize filename and prefix with a UUID to avoid collisions.
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(f.filename).name).strip("._") or "file"
    stored_name = f"{uuid.uuid4().hex}_{safe}"
    target_dir = _comp_dir(comp_id)
    target = target_dir / stored_name
    f.save(target)
    size = target.stat().st_size
    cur = conn.execute(
        "INSERT INTO attachments (composition_id, filename, mime, size_bytes, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (comp_id, f.filename, mime, size, now_iso()),
    )
    conn.commit()
    new_id = cur.lastrowid
    att = conn.execute("SELECT * FROM attachments WHERE id = ?", (new_id,)).fetchone()
    conn.close()
    return jsonify(row_to_dict(att)), 201


@app.get("/api/attachments/<int:att_id>")
def get_attachment(att_id: int):
    conn = get_db()
    att = conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    conn.close()
    if att is None:
        abort(404, "Attachment not found")
    comp_dir = UPLOAD_DIR / str(att["composition_id"])
    # The on-disk filename is prefixed with the UUID we wrote; original is the user-facing label.
    matches = list(comp_dir.glob(f"*_{att['filename']}"))
    if not matches:
        abort(404, "File missing on disk")
    return send_from_directory(comp_dir, matches[0].name, mimetype=att["mime"], as_attachment=False)


@app.delete("/api/attachments/<int:att_id>")
def delete_attachment(att_id: int):
    conn = get_db()
    att = conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if att is None:
        conn.close()
        abort(404, "Attachment not found")
    comp_dir = UPLOAD_DIR / str(att["composition_id"])
    matches = list(comp_dir.glob(f"*_{att['filename']}"))
    conn.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
    conn.commit()
    conn.close()
    for m in matches:
        try:
            m.unlink()
        except OSError:
            pass
    return ("", 204)


# ---- Error handler — always JSON ---- #


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(413)
def _http_error(err):
    # werkzeug provides .description; HTTPException's default str() is fine too.
    msg = getattr(err, "description", str(err))
    return jsonify({"error": msg}), err.code


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("Tabla Compositions DB")
    print(f"  DB:     {DB_PATH}")
    print(f"  Uploads:{UPLOAD_DIR}")
    print(f"  Open:   http://localhost:{PORT}")
    print("=" * 60)
    # debug=False so we don't get the auto-reloader attaching twice in some environments.
    app.run(host="127.0.0.1", port=PORT, debug=False)
