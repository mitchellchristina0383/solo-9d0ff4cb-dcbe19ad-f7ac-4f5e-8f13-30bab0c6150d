# -*- coding: utf-8 -*-
"""Package building tests: contents, tamper detection, atomicity."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile

import pytest

from pitr.core import AllChainsFailed, FileUnavailable, PitrError, parse_lsn
from pitr.store import Catalog, PackageStore, build_package, sha256_file


def make_plan(cat, tli="00000003", lsn_text="0/600000"):
    return cat.plan(tli, parse_lsn(lsn_text))


def test_build_package_contents(catalog, packages_dir):
    plan = make_plan(catalog)
    info = build_package(catalog, plan, packages_dir)
    path = packages_dir / info.filename
    assert path.is_file()
    assert sha256_file(path) == info.sha256
    assert path.stat().st_size == info.size

    with tarfile.open(path, "r:gz") as tar:
        names = tar.getnames()
        assert "README.txt" in names
        assert "pitr_plan.json" in names
        assert "recovery.signal" in names
        assert "postgresql.auto.conf" in names
        # Every WAL file from the plan is present at wal/<name>.
        for step in plan.wal_steps:
            assert f"wal/{step.name}" in names
        # Every base file lands under basebackup/.
        for f in plan.steps[0].files:
            assert f.package_path in names
        meta = json.loads(tar.extractfile("pitr_plan.json").read())
        assert meta["target_lsn"] == "0/600000"
        assert meta["backup"]["id"] == plan.backup.backup_id
        readme = tar.extractfile("README.txt").read().decode()
        assert "重放顺序" in readme
        assert meta["target_lsn"] in readme

        # Bytes inside the archive must match the recorded digests.
        for step in plan.wal_steps:
            member = tar.extractfile(f"wal/{step.name}").read()
            assert hashlib.sha256(member).hexdigest() == sha256_file(
                catalog._full_path(step.files[0].source_rel))


def test_package_copies_corrected_bytes_not_corrupt_primary(catalog, packages_dir):
    plan = make_plan(catalog)
    # Timeline 3 segment 5 primary is corrupt; the package must hold the copy.
    info = build_package(catalog, plan, packages_dir)
    with tarfile.open(packages_dir / info.filename, "r:gz") as tar:
        data = tar.extractfile("wal/000000030000000000000005").read()
    assert not data.startswith(b"CORRUPTED")
    seg = catalog.wal_index["000000030000000000000005"]
    assert hashlib.sha256(data).hexdigest() == seg.sha256


def test_build_refuses_unverified_plan(catalog, packages_dir):
    plan = make_plan(catalog)
    plan.steps[1].verify_error = "simulated"
    with pytest.raises(PitrError, match="未全部通过校验"):
        build_package(catalog, plan, packages_dir)
    assert not list(packages_dir.glob("*.tar.gz"))


def test_build_aborts_when_file_changes_after_planning(catalog, packages_dir):
    plan = make_plan(catalog)
    # Corrupt a file after planning has verified it; build must re-hash.
    target = catalog._full_path(plan.wal_steps[0].files[0].source_rel)
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"x")
        with pytest.raises(FileUnavailable):
            build_package(catalog, plan, packages_dir)
        assert not list(packages_dir.glob("*.tar.gz"))
        assert not list(packages_dir.glob(".*.part"))
    finally:
        target.write_bytes(original)


def test_package_store_lists_and_gets(catalog, packages_dir):
    info = build_package(catalog, make_plan(catalog), packages_dir)
    store = PackageStore(packages_dir)
    listed = store.list()
    assert [p.package_id for p in listed] == [info.package_id]
    got, path, meta = store.get(info.package_id)
    assert got.sha256 == info.sha256
    assert path.is_file()
    assert meta["plan"]["target_lsn"] == "0/600000"
    assert store.get(info.package_id)[0].step_count == len(make_plan(catalog).steps)


def test_package_store_prunes_expired(catalog, packages_dir):
    build_package(catalog, make_plan(catalog), packages_dir, now=1000.0)
    store = PackageStore(packages_dir, ttl_seconds=60)
    assert store.prune(now=1100.0) == 2  # archive + metadata
    assert not list(packages_dir.glob("*.tar.gz"))


def test_package_store_rejects_bad_ids(packages_dir):
    store = PackageStore(packages_dir)
    with pytest.raises(Exception):
        store.get("../../etc/passwd")


def test_wal_missing_from_catalogue_is_gap_not_crash(sample_dir, tmp_path):
    broken = tmp_path / "broken"
    shutil.copytree(sample_dir, broken)
    raw = json.loads((broken / "wal_manifest.json").read_text())
    raw["segments"] = [s for s in raw["segments"]
                       if s["file_name"] != "000000030000000000000006"]
    (broken / "wal_manifest.json").write_text(json.dumps(raw))
    cat = Catalog.load(broken)
    with pytest.raises(AllChainsFailed) as exc:
        cat.plan("00000003", parse_lsn("0/700000"))
    gaps = [a for a in exc.value.attempts if a.kind == "gap"]
    assert gaps and gaps[0].gap.file_name == "000000030000000000000006"


def test_corrupt_wal_without_copy_falls_back_to_older_backup(sample_dir, tmp_path):
    """If the copy disappears, the plan must reselect an earlier base backup.

    Timeline 2 segment 4 is needed by every timeline-2/3 target.  Corrupting it
    with no copy makes the newest backups unusable, but base_20251120_0100 on
    timeline 1 can still reach a timeline-2 target because the fork is at
    0/300000 and segment 3 exists on both timelines.
    """
    broken = tmp_path / "broken-wal"
    shutil.copytree(sample_dir, broken)
    raw = json.loads((broken / "wal_manifest.json").read_text())
    for seg in raw["segments"]:
        if seg["file_name"] == "000000020000000000000004":
            seg["copies"] = []
    (broken / "wal_manifest.json").write_text(json.dumps(raw))
    (broken / "wal" / "000000020000000000000004").write_bytes(b"RUINED")
    cat = Catalog.load(broken)
    with pytest.raises(AllChainsFailed) as exc:
        cat.plan("00000002", parse_lsn("0/480000"))
    kinds = {a.backup.backup_id: a.kind for a in exc.value.attempts}
    assert kinds["base_20251122_0100"] == "corrupt"
    assert kinds["base_20251120_0100"] == "corrupt"
    corrupt_wal = [a for a in exc.value.attempts if a.kind == "corrupt"
                   and a.files and a.files[0].startswith("wal/")]
    assert corrupt_wal, "the ruined WAL file must be reported as corrupt"
    assert "000000020000000000000004" in corrupt_wal[0].message
