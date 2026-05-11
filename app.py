"""
app.py — AskMyCFO Flask web application.

Routes:
  GET  /                       upload page
  POST /upload                 run pipeline → redirect to /job/<id>
  GET  /job/<id>               side-by-side comparison view
  GET  /job/<id>/file/<name>   serve trimmed PDF or any CSV in the job dir
  GET  /jobs                   list of past jobs
  GET  /healthz                JSON health check (no collision with /reports)
"""
from __future__ import annotations
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect, url_for,
    send_from_directory, abort, jsonify,
)

from core.classifier import load_mapping
from core.pipeline import run_pipeline


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
JOBS_DIR = DATA_DIR / "jobs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {".pdf"}
MAX_UPLOAD_MB = 200

# Pre-load mapping at startup (one-time YAML parse, then reuse)
MAPPING = load_mapping()


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True


# ─── Helpers ───────────────────────────────────────────────────────────────

def _new_job_id() -> str:
    """Short, URL-safe, sortable-ish job id."""
    return secrets.token_urlsafe(6).replace("_", "x").replace("-", "y")[:8].lower()


def _list_jobs():
    """Return job summaries newest-first."""
    out = []
    for job_path in sorted(JOBS_DIR.iterdir(), reverse=True):
        if not job_path.is_dir():
            continue
        diag = job_path / "diagnostics.json"
        if not diag.exists():
            continue
        try:
            d = json.loads(diag.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        meta = (job_path / "meta.json")
        meta_d = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
        out.append({
            "id": job_path.name,
            "filename": meta_d.get("filename", "unknown.pdf"),
            "uploaded_at": meta_d.get("uploaded_at", ""),
            "pages": [p["page"] for p in d.get("pages_located", [])],
            "balanced_cy": d.get("balance_check", {}).get("balanced_cy"),
            "balanced_py": d.get("balance_check", {}).get("balanced_py"),
            "total_assets_cy": d.get("totals", {}).get("total_assets", [0, 0])[0],
            "difference_cy": d.get("totals", {}).get("difference", [0, 0])[0],
        })
    return out


def _format_inr(value: float) -> str:
    """Indian comma format: 1,23,45,678.90"""
    if value is None:
        return "—"
    neg = value < 0
    s = f"{abs(value):,.2f}"
    # Re-do the comma grouping in Indian style (last 3, then 2s)
    parts = s.split(".")
    intp = parts[0].replace(",", "")
    if len(intp) > 3:
        last3 = intp[-3:]
        rest = intp[:-3]
        # Group rest in 2s from right
        groups = []
        while len(rest) > 2:
            groups.append(rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.append(rest)
        intp = ",".join(reversed(groups)) + "," + last3
    out = intp + ("." + parts[1] if len(parts) > 1 else "")
    return ("(" + out + ")") if neg else out


@app.template_filter("inr")
def inr_filter(v):
    return _format_inr(v)


@app.template_filter("pct")
def pct_filter(v):
    return f"{v:.0f}%" if v is not None else "—"


# ─── Routes ────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    """Landing / upload page."""
    return render_template("upload.html", recent_jobs=_list_jobs()[:6])


@app.route("/upload", methods=["POST"])
def upload():
    """Receive a PDF and run the pipeline."""
    f = request.files.get("pdf")
    if not f or not f.filename:
        return render_template("upload.html", error="Please choose a PDF file.",
                                recent_jobs=_list_jobs()[:6]), 400

    suffix = Path(f.filename).suffix.lower()
    if suffix not in ALLOWED_EXT:
        return render_template("upload.html",
                                error=f"File type {suffix!r} not supported (PDF only).",
                                recent_jobs=_list_jobs()[:6]), 400

    job_id = _new_job_id()
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Save upload
    saved_pdf = job_dir / "source.pdf"
    f.save(str(saved_pdf))

    # Persist metadata
    meta = {
        "filename":    f.filename,
        "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        "size_bytes":  saved_pdf.stat().st_size,
    }
    (job_dir / "meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    # Run pipeline
    try:
        result = run_pipeline(str(saved_pdf), job_dir, mapping=MAPPING)
    except Exception as e:
        (job_dir / "error.txt").write_text(str(e), encoding="utf-8")
        return render_template("upload.html",
                                error=f"Pipeline error: {e}",
                                recent_jobs=_list_jobs()[:6]), 500

    if not result.get("ok"):
        (job_dir / "error.txt").write_text(
            result.get("error", "Unknown error"), encoding="utf-8"
        )
        return render_template("upload.html",
                                error=result.get("error", "Pipeline failed"),
                                recent_jobs=_list_jobs()[:6]), 422

    return redirect(url_for("job_view", job_id=job_id))


@app.route("/job/<job_id>", methods=["GET"])
def job_view(job_id):
    """Render the side-by-side comparison page."""
    job_dir = JOBS_DIR / job_id
    if not job_dir.is_dir():
        abort(404)

    diag_path = job_dir / "diagnostics.json"
    if not diag_path.exists():
        abort(404)

    diag = json.loads(diag_path.read_text(encoding="utf-8"))
    meta_path = job_dir / "meta.json"
    meta = (json.loads(meta_path.read_text(encoding="utf-8"))
             if meta_path.exists() else {})

    # Raw rows preview — load the CSV header + first N rows for the tab
    raw_csv_path = job_dir / "raw_rows.csv"
    raw_preview = []
    if raw_csv_path.exists():
        import csv
        with raw_csv_path.open(encoding="utf-8") as f:
            r = csv.reader(f)
            try:
                next(r)  # header
                for i, row in enumerate(r):
                    raw_preview.append(row)
                    if i > 400:
                        break
            except StopIteration:
                pass

    return render_template(
        "compare.html",
        job_id=job_id,
        meta=meta,
        diag=diag,
        raw_preview=raw_preview,
        pdf_url=url_for("job_file", job_id=job_id, name="source_BS_PL.pdf"),
        template_csv_url=url_for("job_file", job_id=job_id, name="template.csv"),
        raw_csv_url=url_for("job_file", job_id=job_id, name="raw_rows.csv"),
        diag_url=url_for("job_file", job_id=job_id, name="diagnostics.json"),
    )


@app.route("/job/<job_id>/file/<path:name>", methods=["GET"])
def job_file(job_id, name):
    """Serve a file from inside a job dir (PDF, CSVs, JSON)."""
    job_dir = JOBS_DIR / job_id
    if not job_dir.is_dir():
        abort(404)
    # Block path traversal
    safe_name = Path(name).name
    target = job_dir / safe_name
    if not target.exists() or not target.is_file():
        abort(404)
    return send_from_directory(
        str(job_dir.resolve()), safe_name,
        as_attachment=request.args.get("download") == "1",
    )


@app.route("/jobs", methods=["GET"])
def jobs():
    """Past jobs listing."""
    return render_template("jobs.html", jobs=_list_jobs())


@app.route("/healthz", methods=["GET"])
def healthz():
    """Health check (avoiding the /health name collision noted in your memory)."""
    return jsonify({
        "ok": True,
        "buckets_loaded_bs": len(MAPPING.bs_buckets),
        "buckets_loaded_pl": len(MAPPING.pl_buckets),
        "aliases_bs":        len(MAPPING.bs_index),
        "aliases_pl":        len(MAPPING.pl_index),
    })


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
