# -*- coding: utf-8 -*-
"""Flask application: PITR recovery-package builder for the on-call DBA.

Routes
------
GET  /                     dashboard: timelines, backups, target form, history
POST /plan                 build a verified replay chain for a target
GET  /packages             list published packages
GET  /packages/<id>/download  download a verified package
GET  /healthz              readiness probe used by Docker Compose
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import core
from .core import (
    AllChainsFailed,
    ManifestError,
    PitrError,
    PlanningError,
    format_lsn,
    parse_lsn,
)
from .store import Catalog, NotFound, PackageStore, build_package, sha256_file


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).resolve()


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATA_DIR=_env_path("PITR_DATA_DIR", "/data"),
        PACKAGES_DIR=_env_path("PITR_PACKAGES_DIR", "/packages"),
        PACKAGE_TTL=int(os.environ.get("PITR_PACKAGE_TTL", str(24 * 3600))),
        SECRET_KEY=os.environ.get("PITR_SECRET_KEY", "pitr-dev-only"),
        MAX_CONTENT_LENGTH=1 * 1024 * 1024,
        ALLOW_EMPTY_SECRET=os.environ.get("PITR_ALLOW_DEV_SECRET", "") == "1",
    )
    if app.config["SECRET_KEY"] == "pitr-dev-only" and not app.config["ALLOW_EMPTY_SECRET"]:
        # A stable, non-secret default is fine for flash messages; we only warn
        # loudly so an operator does not mistake it for real session security.
        app.logger.warning(
            "PITR_SECRET_KEY 未设置，正在使用开发默认值（仅用于 flash 消息）"
        )

    store = PackageStore(app.config["PACKAGES_DIR"], app.config["PACKAGE_TTL"])
    app.extensions["pitr_store"] = store
    app.extensions["pitr_catalog"] = None

    @app.template_filter("lsn")
    def _lsn_filter(value):
        if value is None:
            return "—"
        try:
            return format_lsn(int(value))
        except (TypeError, ValueError):
            return "—"

    @app.template_filter("datetime")
    def _datetime_filter(value):
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(value)))
        except (TypeError, ValueError, OSError):
            return "—"

    @app.template_filter("filesize")
    def _filesize_filter(value):
        try:
            n = float(value)
        except (TypeError, ValueError):
            return "—"
        for unit in ("B", "KiB", "MiB", "GiB"):
            if abs(n) < 1024 or unit == "GiB":
                return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
            n /= 1024
        return f"{n:.1f} GiB"

    def catalog() -> Catalog:
        cat = app.extensions.get("pitr_catalog")
        if cat is None:
            cat = Catalog.load(app.config["DATA_DIR"])
            app.extensions["pitr_catalog"] = cat
        return cat

    @app.before_request
    def _prune() -> None:
        if request.endpoint == "index":
            try:
                store.prune()
            except OSError:
                pass

    # -- dashboard --------------------------------------------------------

    @app.get("/")
    def index():
        try:
            cat = catalog()
        except (ManifestError, OSError) as exc:
            return render_template(
                "error.html",
                title="清单加载失败",
                message=str(exc),
                data_dir=str(app.config["DATA_DIR"]),
            ), 500

        timeline_rows = []
        for tli in sorted(cat.timelines, key=lambda t: int(t, 16)):
            t = cat.timelines[tli]
            timeline_rows.append({
                "timeline": tli,
                "parent": t.parent,
                "switchpoint": format_lsn(t.start_lsn) if t.parent else "—",
                "range": cat.timeline_range_text(tli),
                "is_target": True,
            })

        wal_rows = []
        for seg in cat.wal:
            wal_rows.append({
                "file_name": seg.file_name,
                "timeline": seg.timeline,
                "segno": seg.segno,
                "start": format_lsn(core.segno_start(seg.segno, cat.segment_size)),
                "end": format_lsn(
                    core.segno_start(seg.segno, cat.segment_size)
                    + cat.segment_size - 1),
                "sha256": seg.sha256,
                "copies": len(seg.copies),
                "size": seg.size,
            })

        backups = []
        for b in sorted(cat.backups, key=lambda b: (b.consistent_lsn, b.timeline_int)):
            backups.append({
                "id": b.backup_id,
                "label": b.label,
                "timeline": b.timeline,
                "start": format_lsn(b.start_lsn),
                "consistent": format_lsn(b.consistent_lsn),
                "files": len(b.files),
            })

        return render_template(
            "index.html",
            timelines=timeline_rows,
            wal=wal_rows,
            backups=backups,
            suggestions=cat.suggestions(),
            segment_size=cat.segment_size,
            packages=store.list(),
            data_dir=str(app.config["DATA_DIR"]),
            loaded_at=cat.loaded_at,
        )

    @app.get("/healthz")
    def healthz():
        try:
            cat = catalog()
        except Exception as exc:  # noqa: BLE001 - health endpoint reports any fault
            return jsonify({"status": "error", "detail": str(exc)}), 503
        return jsonify({
            "status": "ok",
            "timelines": len(cat.timelines),
            "backups": len(cat.backups),
            "segments": len(cat.wal),
        })

    @app.post("/reload")
    def reload_catalog():
        app.extensions["pitr_catalog"] = None
        flash("清单已重新加载", "ok")
        return redirect(url_for("index"))

    # -- planning ---------------------------------------------------------

    @app.post("/plan")
    def plan():
        target_timeline = (request.form.get("target_timeline") or "").strip().upper()
        target_text = (request.form.get("target_lsn") or "").strip()

        if not target_timeline or not target_text:
            flash("请填写目标时间线和目标 LSN", "err")
            return redirect(url_for("index"))
        try:
            target_lsn = parse_lsn(target_text)
        except ManifestError as exc:
            flash(str(exc), "err")
            return redirect(url_for("index"))

        try:
            cat = catalog()
            recovery = cat.plan(target_timeline, target_lsn)
        except AllChainsFailed as exc:
            return render_template(
                "failed.html",
                title="无法生成重放链",
                message=str(exc),
                attempts=exc.attempts,
                target_timeline=target_timeline,
                target_lsn=target_text,
                data_dir=str(app.config["DATA_DIR"]),
            ), 422
        except (PlanningError, ManifestError) as exc:
            return render_template(
                "failed.html",
                title="目标不可用",
                message=str(exc),
                attempts=[],
                target_timeline=target_timeline,
                target_lsn=target_text,
                data_dir=str(app.config["DATA_DIR"]),
            ), 400

        # The plan is only offered for download once every byte verified.
        return render_template(
            "plan.html",
            plan=recovery,
            target_timeline=target_timeline,
            target_lsn=format_lsn(target_lsn),
            steps=recovery.steps,
            forks=recovery.forks(),
            copies=recovery.copied_files,
            skipped=recovery.attempts,
            can_build=recovery.verified,
        )

    @app.post("/build")
    def build():
        target_timeline = (request.form.get("target_timeline") or "").strip().upper()
        target_text = (request.form.get("target_lsn") or "").strip()
        try:
            target_lsn = parse_lsn(target_text)
        except ManifestError as exc:
            flash(str(exc), "err")
            return redirect(url_for("index"))
        try:
            cat = catalog()
            recovery = cat.plan(target_timeline, target_lsn)
        except AllChainsFailed as exc:
            return render_template(
                "failed.html", title="无法生成重放链", message=str(exc),
                attempts=exc.attempts, target_timeline=target_timeline,
                target_lsn=target_text, data_dir=str(app.config["DATA_DIR"]),
            ), 422
        except (PlanningError, ManifestError) as exc:
            return render_template(
                "failed.html", title="目标不可用", message=str(exc),
                attempts=[], target_timeline=target_timeline,
                target_lsn=target_text, data_dir=str(app.config["DATA_DIR"]),
            ), 400

        if not recovery.verified:
            return render_template(
                "plan.html", plan=recovery, target_timeline=target_timeline,
                target_lsn=format_lsn(target_lsn),
                steps=recovery.steps,
                forks=recovery.forks(), copies=recovery.copied_files,
                skipped=recovery.attempts, can_build=False,
            ), 409

        try:
            info = build_package(cat, recovery, store.dir)
        except PitrError as exc:
            app.logger.exception("package build failed")
            return render_template(
                "failed.html", title="打包失败", message=str(exc),
                attempts=[], target_timeline=target_timeline,
                target_lsn=target_text, data_dir=str(app.config["DATA_DIR"]),
            ), 500
        flash(f"已生成并校验交付包 {info.filename}", "ok")
        return redirect(url_for("package_detail", package_id=info.package_id))

    # -- packages ---------------------------------------------------------

    @app.get("/packages")
    def package_list():
        return render_template("packages.html", packages=store.list())

    @app.get("/packages/<package_id>")
    def package_detail(package_id: str):
        try:
            info, path, meta = store.get(package_id)
        except NotFound as exc:
            abort(404, str(exc))
        steps = meta.get("plan", {}).get("steps", [])
        try:
            current_digest = sha256_file(path) if path.is_file() else None
        except OSError:
            current_digest = None
        return render_template(
            "package.html", info=info, steps=steps,
            warnings=meta.get("plan", {}).get("warnings", []),
            package_ok=(current_digest == info.sha256),
        )

    @app.get("/packages/<package_id>/download")
    def package_download(package_id: str):
        try:
            info, path, _meta = store.get(package_id)
        except NotFound as exc:
            abort(404, str(exc))
        if not path.is_file():
            abort(404, "包文件已过期，请重新生成")
        # Re-verify the archive digest before handing it out; a package is only
        # downloadable when its bytes still match the recorded digest.
        if sha256_file(path) != info.sha256:
            abort(409, "包文件校验失败，请重新生成")
        return send_file(
            path,
            mimetype="application/gzip",
            as_attachment=True,
            download_name=info.filename,
            etag=info.sha256,
        )

    # -- errors -----------------------------------------------------------

    @app.errorhandler(404)
    def not_found(exc):
        return render_template("error.html", title="未找到",
                               message=getattr(exc, "description", "页面不存在"),
                               data_dir=str(app.config["DATA_DIR"])), 404

    @app.errorhandler(409)
    def conflict(exc):
        return render_template("error.html", title="校验失败",
                               message=getattr(exc, "description", "校验失败"),
                               data_dir=str(app.config["DATA_DIR"])), 409

    @app.errorhandler(500)
    def server_error(exc):
        return render_template("error.html", title="服务器错误",
                               message="内部错误，请查看服务日志",
                               data_dir=str(app.config["DATA_DIR"])), 500

    return app



app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
