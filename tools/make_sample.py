#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the local sample archive used by the PITR web app.

The dataset is deterministic so tests and screenshots stay stable.  It models
three timelines with two forks, five base backups and an archived WAL range,
and deliberately plants three archive defects:

* WAL ``000000030000000000000005`` (timeline 3, segment 5) is corrupted on
  disk but a verified duplicate exists under ``copies/``;
* ``base_20251122_0100/global/pg_control`` is corrupted with a duplicate;
* ``base_20251121_0100/base/1/2610`` is corrupted with **no** duplicate, so the
  closest backup must be abandoned and an older one reselected.

Timeline 3 has no segment 7, which creates an honest archive gap for late
targets (the web app must then report the breakpoint).

Run:  python3 tools/make_sample.py --out data
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

SEGMENT_SIZE = 1024 * 1024
MB = 0x100000

TLI1 = "00000001"
TLI2 = "00000002"
TLI3 = "00000003"
FORK_1_2 = 0x300000  # 3 MiB, inside segment 3
FORK_2_3 = 0x580000  # 5.5 MiB, inside segment 5


def lsn(value: int) -> str:
    return f"{value >> 32:X}/{value & 0xFFFFFFFF:X}"


def wal_name(tli: str, segno: int) -> str:
    per_log = 0x100000000 // SEGMENT_SIZE
    log_id, seg = divmod(segno, per_log)
    return f"{tli}{log_id:08X}{seg:08X}"


def filler(seed: str, length: int) -> bytes:
    """Deterministic pseudo-random-ish payload (fast, compressible, unique)."""
    base = (f"[sample:{seed}] ").encode()
    chunk = (base * ((65536 // len(base)) + 1))[:65536]
    reps, rest = divmod(length, len(chunk))
    return chunk * reps + chunk[:rest]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wal_bytes(tli: str, segno: int) -> bytes:
    name = wal_name(tli, segno)
    header = (
        f"PostgreSQL WAL segment sample\nfile={name}\ntimeline={tli}\n"
        f"segno={segno}\nrange={lsn(segno * SEGMENT_SIZE)}-"
        f"{lsn((segno + 1) * SEGMENT_SIZE - 1)}\n"
    ).encode()
    body = filler(f"wal-{name}", SEGMENT_SIZE - len(header))
    return header + body


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def dump_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def payload_files(backup_id: str) -> dict[str, bytes]:
    """Small, realistic-looking base backup payload."""
    seed = hashlib.sha256(backup_id.encode()).hexdigest()[:12]
    return {
        "backup_label": (
            f"START WAL LOCATION: {backup_id}\n"
            f"CHECKPOINT LOCATION: {seed}\n"
            f"BACKUP METHOD: streamed\nBACKUP FROM: primary\n"
            f"START TIME: 2025-11-23 01:00:00 CST\nLABEL: {backup_id}\n"
            f"START TIMELINE: 1\n"
        ).encode(),
        "global/pg_control": (
            f"sample pg_control for {backup_id}\nchecksum={seed}\n"
            f"state=shutdown\n"
        ).encode(),
        "base/1/2610": filler(f"catalog-{backup_id}", 48 * 1024),
        "base/1/16384": filler(f"table-{backup_id}", 96 * 1024),
        "postgresql.conf": (
            "# sample configuration snapshot\n"
            f"# backup {backup_id}\nwal_level = replica\narchive_mode = on\n"
            "max_wal_senders = 4\n"
        ).encode(),
    }


BACKUPS = [
    {
        "id": "base_20251120_0100",
        "timeline": TLI1,
        "replay_start": 0x080000,
        "consistency": 0x180000,
        "label": "11-20 01:00 全备（时间线 1）",
        "corrupt": None,
        "copy": None,
    },
    {
        "id": "base_20251121_0100",
        "timeline": TLI1,
        "replay_start": 0x200000,
        "consistency": 0x2C0000,
        "label": "11-21 01:00 全备（时间线 1）",
        "corrupt": "base/1/2610",
        "copy": None,  # corrupt with no usable duplicate
    },
    {
        "id": "base_20251122_0100",
        "timeline": TLI2,
        "replay_start": 0x320000,
        "consistency": 0x3C0000,
        "label": "11-22 01:00 全备（时间线 2）",
        "corrupt": "global/pg_control",
        "copy": True,  # corrupt but duplicated under copies/
    },
    {
        "id": "base_20251123_0100",
        "timeline": TLI2,
        "replay_start": 0x480000,
        "consistency": 0x540000,
        "label": "11-23 01:00 全备（时间线 2）",
        "corrupt": "global/pg_control",
        "copy": False,  # corrupt, no duplicate: forces reselection
    },
    {
        "id": "base_20251123_0900",
        "timeline": TLI3,
        "replay_start": 0x5C0000,
        "consistency": 0x620000,
        "label": "11-23 09:00 全备（时间线 3）",
        "corrupt": None,
        "copy": None,
    },
]

WAL_CATALOGUE = [
    (TLI1, 0), (TLI1, 1), (TLI1, 2), (TLI1, 3),
    (TLI2, 3), (TLI2, 4), (TLI2, 5),
    (TLI3, 5), (TLI3, 6),
    # Timeline 3 segment 7 is intentionally absent: a genuine archive gap.
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data", help="output data directory")
    args = ap.parse_args()
    root = Path(args.out).resolve()

    # Only remove the artefacts this script owns, so a mistyped --out cannot
    # delete unrelated data.
    for sub in ("full", "wal", "copies", "manifests"):
        target = root / sub
        if target.exists():
            shutil.rmtree(target)
    for name in ("timelines.json", "backups.json", "wal_manifest.json"):
        p = root / name
        if p.exists():
            p.unlink()
    root.mkdir(parents=True, exist_ok=True)

    dump_json(root / "timelines.json", {
        "dataset": "pitr-sample",
        "notes": "样例归档：3 条时间线，2 次分叉，WAL 段 1 MiB",
        "segment_size": SEGMENT_SIZE,
        "timelines": [
            {"timeline": TLI1, "parent": None, "switchpoint_lsn": None,
             "note": "初始时间线"},
            {"timeline": TLI2, "parent": TLI1,
             "switchpoint_lsn": lsn(FORK_1_2),
             "note": "在段 3 内部分叉（3 MiB）"},
            {"timeline": TLI3, "parent": TLI2,
             "switchpoint_lsn": lsn(FORK_2_3),
             "note": "在段 5 内部分叉（5.5 MiB）"},
        ],
    })

    # ---- base backups ----------------------------------------------------
    backup_entries = []
    for spec in BACKUPS:
        bid = spec["id"]
        files = payload_files(bid)
        manifest_files = []
        for rel, data in files.items():
            digest = sha256_bytes(data)
            copies = []
            primary = root / "full" / bid / rel
            write(primary, data)
            if spec["corrupt"] == rel:
                if spec["copy"]:
                    copy_rel = f"copies/{bid}-{rel.replace('/', '_')}"
                    write(root / copy_rel, data)
                    copies.append(copy_rel)
                    # Corrupt the primary *after* recording the good digest.
                    write(primary, b"CORRUPTED SAMPLE BYTES\n" + filler(
                        f"corrupt-{bid}-{rel}", 4096))
                else:
                    write(primary, b"CORRUPTED SAMPLE BYTES (no duplicate)\n")
            manifest_files.append({
                "path": rel,
                "size": len(data),
                "sha256": digest,
                "copies": copies,
            })
        dump_json(root / "manifests" / f"{bid}.json", {
            "backup_id": bid,
            "timeline": spec["timeline"],
            "files": manifest_files,
        })
        backup_entries.append({
            "id": bid,
            "timeline": spec["timeline"],
            "replay_start_lsn": lsn(spec["replay_start"]),
            "consistency_lsn": lsn(spec["consistency"]),
            "manifest": f"manifests/{bid}.json",
            "label": spec["label"],
        })

    dump_json(root / "backups.json", {"backups": backup_entries})

    # ---- WAL archive -----------------------------------------------------
    segments = []
    for tli, segno in WAL_CATALOGUE:
        name = wal_name(tli, segno)
        data = wal_bytes(tli, segno)
        rel = f"wal/{name}"
        write(root / rel, data)
        copies = []
        if name == "000000030000000000000005":
            copy_rel = f"copies/{name}"
            write(root / copy_rel, data)
            copies.append(copy_rel)
            # Corrupt the primary; the duplicate carries the recorded digest.
            write(root / rel, b"CORRUPTED SAMPLE WAL BYTES\n" + filler(
                f"corrupt-{name}", 65536))
        segments.append({
            "file_name": name,
            "timeline": tli,
            "path": rel,
            "size": len(data),
            "sha256": sha256_bytes(data),
            "copies": copies,
        })

    dump_json(root / "wal_manifest.json", {
        "dataset": "pitr-sample",
        "segment_size": SEGMENT_SIZE,
        "segments": segments,
    })

    total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    print(f"sample written to {root}")
    print(f"files={sum(1 for p in root.rglob('*') if p.is_file())} "
          f"bytes={total} ({total / MB:.1f} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
