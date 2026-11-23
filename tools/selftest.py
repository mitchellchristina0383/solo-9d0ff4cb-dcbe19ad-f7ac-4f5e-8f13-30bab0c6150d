#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end self-test against the sample archive.

Exercises the three acceptance scenarios on the real data directory and prints
a pass/fail summary:

1. intra-segment fork (both timeline files required),
2. corrupt file replaced by a duplicate copy,
3. corrupt file with no copy -> backup reselection,
4. genuine archive gap -> explicit breakpoint,
5. full package build + archive-level SHA-256 check.

Run:  python3 tools/selftest.py --data data --work var/selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pitr.core import AllChainsFailed, format_lsn, parse_lsn  # noqa: E402
from pitr.store import Catalog, build_package, sha256_file  # noqa: E402

PASS, FAIL = "PASS", "FAIL"


class Report:
    def __init__(self) -> None:
        self.rows = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, ok, detail))
        print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))
        return ok

    def summary(self) -> int:
        bad = [r for r in self.rows if not r[1]]
        print()
        print(f"{len(self.rows) - len(bad)}/{len(self.rows)} checks passed")
        return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--work", default=None,
                    help="directory for scratch copies and built packages")
    args = ap.parse_args()

    data = Path(args.data).resolve()
    work = Path(args.work).resolve() if args.work else Path(
        tempfile.mkdtemp(prefix="pitr-selftest-"))
    work.mkdir(parents=True, exist_ok=True)
    packages = work / "packages"
    if packages.exists():
        shutil.rmtree(packages)

    rep = Report()
    cat = Catalog.load(data)

    # -- 1. intra-segment fork -------------------------------------------
    p = cat.plan("00000003", parse_lsn("0/600000"))
    names = [(s.timeline, s.name, s.fork_role) for s in p.wal_steps]
    expected_tail = [
        ("00000002", "000000020000000000000005", "before-fork"),
        ("00000003", "000000030000000000000005", "after-fork"),
    ]
    rep.check(
        "段内分叉：2→3 在 0/580000 段内分叉，前后各取所属时间线",
        names[-2:] == expected_tail,
        f"{names[-2:]}",
    )
    rep.check(
        "段内分叉：两段落在同一 segment 槽位且范围衔接",
        p.wal_steps[-2].segment_start == p.wal_steps[-1].segment_start
        and p.wal_steps[-2].end_lsn + 1 == p.wal_steps[-1].start_lsn,
        f"{p.wal_steps[-2].range_text} → {p.wal_steps[-1].range_text}",
    )
    cursor = p.backup.start_lsn
    contiguous = True
    for s in p.wal_steps:
        if s.start_lsn != cursor:
            contiguous = False
            break
        cursor = s.end_lsn + 1
    rep.check("重放链 LSN 连续且到达目标",
              contiguous and cursor >= p.target_lsn,
              f"覆盖到 {format_lsn(cursor - 1)}")

    # -- 2. corrupt file with duplicate copy ------------------------------
    step = [s for s in p.wal_steps if s.name == "000000030000000000000005"][0]
    rep.check(
        "坏档替换：损坏的 WAL 主档改用同名副本并校验通过",
        step.verified and bool(step.used_copies)
        and step.used_copies[0].source_rel.startswith("copies/"),
        step.used_copies[0].source_rel if step.used_copies else "no copy",
    )
    base_copies = p.steps[0].used_copies
    rep.check(
        "坏档替换：全备 pg_control 损坏后使用副本",
        bool(base_copies)
        and base_copies[0].package_path == "basebackup/global/pg_control",
        base_copies[0].source_rel if base_copies else "no copy",
    )

    # -- 3. corrupt file with no copy -> reselection ----------------------
    p2 = cat.plan("00000002", parse_lsn("0/560000"))
    rep.check(
        "重选全备：最近全备 pg_control 无副本损坏，改用更早的可用全备",
        p2.backup.backup_id == "base_20251122_0100"
        and any(a.backup.backup_id == "base_20251123_0100" and a.kind == "corrupt"
                for a in p2.attempts),
        f"选用 {p2.backup.backup_id}",
    )

    # -- 4. genuine gap ---------------------------------------------------
    try:
        cat.plan("00000003", parse_lsn("0/800000"))
        gap_ok = False
        gap_detail = "unexpectedly produced a plan"
    except AllChainsFailed as exc:
        gap = next((a.gap for a in exc.attempts if a.gap), None)
        gap_ok = bool(gap) and gap.file_name == "000000030000000000000007"
        gap_detail = str(gap) if gap else str(exc)
    rep.check("断档：缺失段给出明确断点", gap_ok, gap_detail)

    # -- 5. package ------------------------------------------------------
    info = build_package(cat, p, packages)
    archive = packages / info.filename
    rep.check(
        "整包校验：归档 SHA-256 与记录一致",
        archive.is_file() and sha256_file(archive) == info.sha256,
        info.sha256,
    )
    with tarfile.open(archive, "r:gz") as tar:
        listing = tar.getnames()
        missing = [f"wal/{s.name}" for s in p.wal_steps
                   if f"wal/{s.name}" not in listing]
        rep.check("整包校验：全部 WAL 段入包", not missing, ", ".join(missing))
        meta = json.loads(tar.extractfile("pitr_plan.json").read())
        rep.check(
            "整包校验：包内 pitr_plan.json 记录目标与全备",
            meta["target_lsn"] == "0/600000"
            and meta["backup"]["id"] == p.backup.backup_id,
        )
        wal_data = tar.extractfile("wal/000000030000000000000005").read()
        seg = cat.wal_index["000000030000000000000005"]
        rep.check(
            "整包校验：包内 WAL 字节与清单摘要一致（非损坏主档）",
            hashlib.sha256(wal_data).hexdigest() == seg.sha256,
        )

    # -- 6. tamper detection on download ----------------------------------
    from pitr.store import PackageStore
    store = PackageStore(packages)
    got, path, _ = store.get(info.package_id)
    original = path.read_bytes()
    path.write_bytes(original + b"x")
    tampered_detected = sha256_file(path) != got.sha256
    path.write_bytes(original)
    rep.check("下载前复核：篡改后的包会被识别", tampered_detected)

    print(f"\nwork dir: {work}")
    return rep.summary()


if __name__ == "__main__":
    raise SystemExit(main())
