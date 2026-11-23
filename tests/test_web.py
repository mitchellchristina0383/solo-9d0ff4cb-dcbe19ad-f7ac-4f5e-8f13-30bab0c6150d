# -*- coding: utf-8 -*-
"""HTTP-level tests through Flask's test client."""

from __future__ import annotations

import hashlib
import json
import re



def post_plan(client, tli, lsn, endpoint="/plan"):
    return client.post(endpoint, data={"target_timeline": tli, "target_lsn": lsn})


def test_dashboard_lists_catalogue(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "时间线历史" in body
    assert "全备清单" in body
    assert "WAL 归档清单" in body
    for name in ("000000010000000000000003", "000000030000000000000006"):
        assert name in body


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["timelines"] == 3
    assert data["backups"] == 5


def test_plan_shows_replay_order_with_fork(client):
    resp = post_plan(client, "00000003", "0/600000")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "重放顺序" in body
    assert "000000020000000000000005" in body
    assert "000000030000000000000005" in body
    assert "分叉前" in body and "分叉后" in body
    assert "全部校验通过" in body
    # Fork detail section lists both sides.
    assert "段内分叉" in body


def test_plan_reports_copy_substitution(client):
    resp = post_plan(client, "00000003", "0/600000")
    body = resp.get_data(as_text=True)
    assert "copies/000000030000000000000005" in body
    assert "副本" in body


def test_gap_target_returns_breakpoint_page(client):
    resp = post_plan(client, "00000003", "0/800000")
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert "断点" in body
    assert "000000030000000000000007" in body
    assert "0/700000" in body


def test_invalid_lsn_is_rejected(client):
    resp = post_plan(client, "00000003", "not-an-lsn", endpoint="/plan_format")
    # Unknown endpoint; use the real one below.
    assert resp.status_code == 404
    resp = post_plan(client, "00000003", "zz/1")
    assert resp.status_code == 302  # flash + redirect back to the form
    follow = client.get(resp.headers["Location"])
    assert "无效的 LSN" in follow.get_data(as_text=True)


def test_target_on_wrong_timeline_is_explained(client):
    resp = post_plan(client, "00000001", "0/400000")
    assert resp.status_code == 400
    body = resp.get_data(as_text=True)
    assert "分叉点" in body or "时间线" in body


def test_build_then_download(client, packages_dir):
    resp = client.post("/build", data={"target_timeline": "00000003",
                                       "target_lsn": "0/600000"})
    assert resp.status_code == 302
    listing = client.get("/packages")
    body = listing.get_data(as_text=True)
    match = re.search(r"/packages/([0-9a-f]{32})", body)
    assert match, body[:2000]
    package_id = match.group(1)

    detail = client.get(f"/packages/{package_id}")
    assert detail.status_code == 200
    assert "已通过校验" in detail.get_data(as_text=True)

    dl = client.get(f"/packages/{package_id}/download")
    assert dl.status_code == 200
    assert dl.headers["Content-Disposition"].startswith("attachment")
    assert dl.data[:2] == b"\x1f\x8b"
    meta = json.loads(
        (packages_dir / (re.search(r"pitr-[^\s<\"]+\.tar\.gz", body)
                         .group(0) + ".json")).read_text())
    assert hashlib.sha256(dl.data).hexdigest() == meta["sha256"]


def test_download_detects_tampered_package(client, packages_dir):
    client.post("/build", data={"target_timeline": "00000003",
                                "target_lsn": "0/600000"})
    listing = client.get("/packages").get_data(as_text=True)
    package_id = re.search(r"/packages/([0-9a-f]{32})", listing).group(1)
    archive = next(packages_dir.glob("*.tar.gz"))
    archive.write_bytes(b"tampered")
    resp = client.get(f"/packages/{package_id}/download")
    assert resp.status_code == 409


def test_build_refused_when_chain_has_gap(client):
    resp = client.post("/build", data={"target_timeline": "00000003",
                                       "target_lsn": "0/800000"})
    assert resp.status_code == 422
    assert "断点" in resp.get_data(as_text=True)
    assert "暂无交付包" not in resp.get_data(as_text=True)


def test_unknown_package_404(client):
    assert client.get("/packages/deadbeef").status_code == 404
