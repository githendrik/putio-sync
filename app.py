#!/usr/bin/env python3
"""putio-sync: Thin web UI for selective folder sync to put.io via rclone rc API."""

import json
import os
import sqlite3
import urllib.request
import urllib.parse
import urllib.error
from flask import Flask, request, jsonify, render_template, make_response

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
DB_PATH = os.environ.get("PUTIO_SYNC_DB", "/opt/putio-sync/sync.db")
RCD_URL = os.environ.get("RCLONE_RCD_URL", "http://localhost:5572")
PUTIO_DEST_ROOT = os.environ.get("PUTIO_DEST_ROOT", "Synced")


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    db = get_db()
    db.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        src TEXT NOT NULL,
        dst TEXT NOT NULL,
        rclone_jobid INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        error TEXT
    )"""
    )
    db.commit()
    db.close()


def rc(method, params=None):
    url = f"{RCD_URL}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


@app.route("/")
def index():
    resp = render_template("index.html", dest_root=PUTIO_DEST_ROOT)
    r = make_response(resp)
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return r


@app.route("/api/remotes")
def remotes():
    result = rc("config/listremotes")
    r = result.get("remotes", [])
    return jsonify({"local": [x for x in r if x not in ("putio", "local")], "putio": "putio"})


@app.route("/api/browse")
def browse():
    remote = request.args.get("remote", "")
    path = request.args.get("path", "")
    result = rc("operations/list", {"fs": remote + ":", "remote": path, "opt": {"dirsOnly": True}})
    dirs = sorted([d["Name"] for d in result.get("list", [])])
    return jsonify({"dirs": dirs})


@app.route("/api/sync", methods=["POST"])
def sync():
    data = request.json
    items = data.get("items", [])
    dst_base = data.get("dst_base", "").strip("/")
    created = []
    for item in items:
        folder = item["path"].rstrip("/").split("/")[-1]
        dst_path = f"{dst_base}/{folder}" if dst_base else folder
        src_fs = f"{item['remote']}:{item['path']}"
        dst_fs = f"putio:{dst_path}"
        result = rc(
            "sync/copy",
            {"srcFs": src_fs, "dstFs": dst_fs, "_async": "true"},
        )
        jobid = result.get("jobid")
        db = get_db()
        cur = db.execute(
            "INSERT INTO jobs (src, dst, rclone_jobid, status) VALUES (?,?,?,?)",
            (src_fs, dst_fs, jobid, "running"),
        )
        db.commit()
        created.append(
            {"id": cur.lastrowid, "src": src_fs, "dst": dst_fs, "rclone_jobid": jobid}
        )
        db.close()
    return jsonify({"jobs": created})


@app.route("/api/status")
def status():
    db = get_db()
    rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
    result = []
    for row in rows:
        job = dict(row)
        if job["status"] == "running" and job["rclone_jobid"]:
            try:
                rc_status = rc("job/status", {"jobid": str(job["rclone_jobid"])})
                if rc_status.get("finished"):
                    job["status"] = "completed" if rc_status.get("success") else "error"
                    job["error"] = rc_status.get("error", "")
                    db.execute(
                        "UPDATE jobs SET status=?, completed_at=CURRENT_TIMESTAMP, error=? WHERE id=?",
                        (job["status"], job.get("error", ""), job["id"]),
                    )
                    db.commit()
                else:
                    prog = rc("core/stats", {"group": f"job/{job['rclone_jobid']}"})
                    stats = prog if isinstance(prog, dict) else {}
                    total_bytes = stats.get("totalBytes", 0)
                    done_bytes = stats.get("bytes", 0)
                    if total_bytes > 0:
                        job["percentage"] = round(done_bytes / total_bytes * 100)
                    else:
                        job["percentage"] = 0
                    job["speed"] = stats.get("speed", 0)
                    job["eta"] = stats.get("eta", 0)
                    job["transferred"] = done_bytes
                    job["total_size"] = total_bytes
                    job["total_transfers"] = stats.get("totalTransfers", 0)
                    job["completed_transfers"] = stats.get("transfers", 0)
                    xfr = stats.get("transferring", [])
                    if xfr:
                        job["current_files"] = [f["name"] for f in xfr]
            except urllib.error.HTTPError as e:
                if e.code == 500:
                    job["status"] = "completed"
                    job["error"] = ""
                    db.execute(
                        "UPDATE jobs SET status=?, completed_at=CURRENT_TIMESTAMP, error=? WHERE id=?",
                        ("completed", "", job["id"]),
                    )
                    db.commit()
            except Exception:
                pass
        result.append(job)
    db.close()
    return jsonify({"jobs": result})


@app.route("/api/job/<int:job_id>/stop", methods=["POST"])
def stop_job(job_id):
    db = get_db()
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row and row["rclone_jobid"]:
        rc("job/stop", {"jobid": str(row["rclone_jobid"])})
        db.execute(
            "UPDATE jobs SET status=?, completed_at=CURRENT_TIMESTAMP WHERE id=?",
            ("stopped", job_id),
        )
        db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/job/<int:job_id>/rerun", methods=["POST"])
def rerun_job(job_id):
    db = get_db()
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        db.close()
        return jsonify({"error": "job not found"}), 404
    result = rc("sync/copy", {"srcFs": row["src"], "dstFs": row["dst"], "_async": "true"})
    jobid = result.get("jobid")
    cur = db.execute(
        "INSERT INTO jobs (src, dst, rclone_jobid, status) VALUES (?,?,?,?)",
        (row["src"], row["dst"], jobid, "running"),
    )
    db.commit()
    new_id = cur.lastrowid
    db.close()
    return jsonify({"id": new_id, "src": row["src"], "dst": row["dst"], "rclone_jobid": jobid})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080)
