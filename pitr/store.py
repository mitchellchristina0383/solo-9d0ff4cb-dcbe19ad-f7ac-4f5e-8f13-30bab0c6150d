# -*- coding: utf-8 -*-
"""Catalog loading, verification and package building.

This layer owns the filesystem.  It reads the manifests into the models from
:mod:`pitr.core`, verifies payload bytes against the recorded SHA-256 (falling
back to a documented duplicate copy when the primary is corrupt), and builds a
tar.gz package only from bytes that verified.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import core
from .core import (
    AllChainsFailed,
    BaseBackup,
    ChainFailure,
    ChainStep,
    FileEntry,
    FileUnavailable,
    GapError,
    ManifestError,
    PackageInfo,
    PitrError,
    PlanningError,
    RecoveryPlan,
    ResolvedFile,
    Timeline,
    WalSegment,
    backup_unusable_reason,
    build_segment_chain,
    candidate_backups,
    format_lsn,
    segno_start,
    validate_target,
)

PACKAGE_META_SUFFIX = ".json"
DEFAULT_PACKAGE_TTL = 24 * 3600


class NotFound(PitrError):
    pass


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

class Catalog:
    """Immutable snapshot of the sample archive manifests."""

    def __init__(self, data_root: Path):
        self.data_root = Path(data_root).resolve()
        self.segment_size: int = 0
        self.timelines: Dict[str, Timeline] = {}
        self.backups: List[BaseBackup] = []
        self.wal: List[WalSegment] = []
        self.wal_index: Dict[str, WalSegment] = {}
        self.loaded_at: float = 0.0
        self.errors: List[str] = []

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, data_root: Path) -> "Catalog":
        cat = cls(data_root)
        cat.reload()
        return cat

    def reload(self) -> None:
        root = self.data_root
        timelines_raw = self._read_json(root / "timelines.json")
        self.segment_size, self.timelines = core.parse_timelines(timelines_raw)

        backups_raw = self._read_json(root / "backups.json")
        loaded_backups = core.parse_backups(backups_raw, self.timelines)
        backups: List[BaseBackup] = []
        for b in loaded_backups:
            manifest_raw = self._read_json(root / b.manifest)
            backups.append(core.parse_backup_manifest(manifest_raw, b))
        self.backups = backups

        wal_raw = self._read_json(root / "wal_manifest.json")
        manifest_seg = wal_raw.get("segment_size")
        if manifest_seg is not None and manifest_seg != self.segment_size:
            raise ManifestError(
                f"wal_manifest.json 的 segment_size={manifest_seg} 与 "
                f"timelines.json 的 {self.segment_size} 不一致"
            )
        self.wal = core.parse_wal(wal_raw, self.segment_size, self.timelines)
        self.wal_index = {s.file_name: s for s in self.wal}
        self.loaded_at = time.time()

    def _read_json(self, path: Path) -> dict:
        if not path.is_file():
            raise ManifestError(f"清单文件缺失：{path.name}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestError(f"{path.name} 不是合法 JSON：{exc}") from exc
        if not isinstance(raw, dict):
            raise ManifestError(f"{path.name} 顶层必须是 JSON 对象")
        return raw

    # -- lookups ----------------------------------------------------------

    def backup(self, backup_id: str) -> BaseBackup:
        for b in self.backups:
            if b.backup_id == backup_id:
                return b
        raise NotFound(f"未知全备 {backup_id}")

    def wal_segment(self, name: str) -> WalSegment:
        seg = self.wal_index.get(name)
        if seg is None:
            raise NotFound(f"未知 WAL 段 {name}")
        return seg

    def timeline_range_text(self, tli: str) -> str:
        children = core.timeline_children(tli, self.timelines)
        start = self.timelines[tli].start_lsn
        if children:
            end = min(c.switchpoint for c in children if c.switchpoint is not None)
            return f"{format_lsn(start)} – {format_lsn(end - 1)}"
        return f"{format_lsn(start)} 起"

    def suggestions(self) -> List[Dict[str, str]]:
        """Operator-friendly target examples derived from the data."""
        out = []
        seen_tli = set()
        for tli in sorted(self.timelines, key=lambda t: int(t, 16)):
            if tli in seen_tli:
                continue
            children = core.timeline_children(tli, self.timelines)
            if children:
                end = min(c.switchpoint for c in children if c.switchpoint is not None)
                point = max(self.timelines[tli].start_lsn, end - 0x10000)
            else:
                segs = [s for s in self.wal if s.timeline == tli]
                point = segno_start(segs[-1].segno, self.segment_size) + 0x200000 if segs else 0
                if segs:
                    point = min(point, segno_start(segs[-1].segno, self.segment_size)
                                + self.segment_size - 1)
            if point <= 0:
                continue
            out.append({
                "timeline": tli,
                "lsn": format_lsn(point),
                "label": f"时间线 {tli} · {format_lsn(point)}",
            })
            seen_tli.add(tli)
        return out

    # -- file resolution --------------------------------------------------

    def _resolve(self, candidates: Sequence[Tuple[str, str, str, Optional[int]]],
                 package_path: str, original_rel: str) -> ResolvedFile:
        """Pick the first candidate whose bytes match the expected digest.

        ``candidates`` items are ``(source_rel, expected_sha256, label, size)``.
        Raises :class:`FileUnavailable` with every failure reason when none
        matches.
        """
        reasons: List[str] = []
        expected = candidates[0][1].lower()
        for source_rel, _expected, label, size in candidates:
            path = core.safe_rel_path(source_rel, "path")
            full = self._full_path(path)
            if not full.is_file():
                reasons.append(f"{label} {path} 缺失")
                continue
            try:
                actual = sha256_file(full)
            except OSError as exc:
                reasons.append(f"{label} {path} 读取失败：{exc.strerror or exc}")
                continue
            if actual != expected:
                reasons.append(f"{label} {path} SHA-256 不匹配")
                continue
            actual_size = full.stat().st_size
            if size is not None and actual_size != size:
                reasons.append(
                    f"{label} {path} 大小 {actual_size} 与清单 {size} 不一致"
                )
                continue
            return ResolvedFile(
                source_rel=path,
                package_path=package_path,
                sha256=expected,
                size=actual_size,
                original_rel=original_rel,
                used_copy=(path != original_rel),
            )
        raise FileUnavailable(original_rel, expected, reasons)

    def _full_path(self, rel: str) -> Path:
        rel = core.safe_rel_path(rel, "path")
        root = self.data_root
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ManifestError(f"路径越出数据目录：{rel}") from exc
        return target

    def resolve_wal(self, seg: WalSegment) -> ResolvedFile:
        original = seg.path or f"wal/{seg.file_name}"
        candidates = [(original, seg.sha256, "主档", seg.size)]
        for copy_rel in seg.copies:
            candidates.append((copy_rel, seg.sha256, "副本", seg.size))
        return self._resolve(candidates, f"wal/{seg.file_name}", original)

    def resolve_backup_file(self, backup: BaseBackup, entry: FileEntry) -> ResolvedFile:
        original = f"{backup.dir_rel}/{entry.path}"
        candidates = [(original, entry.sha256, "主档", entry.size)]
        for copy_rel in entry.copies:
            candidates.append((copy_rel, entry.sha256, "副本", entry.size))
        return self._resolve(candidates, f"basebackup/{entry.path}", original)

    # -- planning ---------------------------------------------------------

    def plan(self, target_timeline: str, target_lsn: int) -> RecoveryPlan:
        """Build a fully verified plan, falling back to older backups on failure.

        Raises :class:`AllChainsFailed` when every candidate fails; the
        exception carries per-backup failures including explicit gap points.
        """
        validate_target(target_timeline, target_lsn, self.timelines)
        usable = candidate_backups(self.backups, target_lsn, target_timeline,
                                   self.timelines)
        excluded = []
        for b in self.backups:
            if b in usable:
                continue
            # Only mention backups that could plausibly serve this target
            # branch; unrelated timelines would just add noise.
            ancestry = core.timeline_ancestry(target_timeline, self.timelines)
            if b.timeline not in ancestry:
                continue
            reason = backup_unusable_reason(b, target_lsn, target_timeline,
                                            self.timelines)
            excluded.append((b, reason or "不可用"))
        if not usable:
            raise AllChainsFailed(
                f"没有全备的可用于目标 {format_lsn(target_lsn)}"
                f"（时间线 {target_timeline}）",
                [],
            )

        attempts: List[ChainFailure] = []
        for backup in usable:
            try:
                plan = self._plan_with(backup, target_timeline, target_lsn)
            except GapError as exc:
                attempts.append(ChainFailure(
                    backup, "gap", str(exc), gap=exc))
                continue
            except FileUnavailable as exc:
                attempts.append(ChainFailure(
                    backup, "corrupt", str(exc), files=[exc.path]))
                continue
            except (ManifestError, PlanningError) as exc:
                attempts.append(ChainFailure(backup, "error", str(exc)))
                continue
            plan.attempts = attempts
            plan.warnings.extend(
                f"已跳过全备 {b.backup_id}：{reason}" for b, reason in excluded
            )
            for failure in attempts:
                plan.warnings.append(
                    f"已放弃全备 {failure.backup.backup_id}：{failure.message}"
                )
            return plan

        raise AllChainsFailed(
            f"所有候选全备都无法构成完整可校验的重放链（目标 "
            f"{format_lsn(target_lsn)}，时间线 {target_timeline}）",
            attempts,
        )

    def _plan_with(self, backup: BaseBackup, target_timeline: str,
                   target_lsn: int) -> RecoveryPlan:
        plan = RecoveryPlan(
            target_timeline=target_timeline,
            target_lsn=target_lsn,
            segment_size=self.segment_size,
            backup=backup,
        )

        # Resolve (and thereby verify) every base backup payload file first.
        base_files = [self.resolve_backup_file(backup, entry) for entry in backup.files]
        plan.steps.append(
            ChainStep(
                kind="base",
                name=backup.backup_id,
                timeline=backup.timeline,
                start_lsn=backup.start_lsn,
                end_lsn=backup.consistent_lsn,
                files=base_files,
                note=(
                    f"重放起点 {format_lsn(backup.start_lsn)}；"
                    f"一致位点 {format_lsn(backup.consistent_lsn)}"
                ),
            )
        )

        available = {(s.timeline, s.segno) for s in self.wal}
        refs = build_segment_chain(
            backup, target_lsn, target_timeline, self.timelines,
            self.segment_size, available,
        )

        for ref in refs:
            seg = self.wal_index.get(ref.file_name)
            if seg is None or seg.timeline != ref.timeline or seg.segno != ref.segno:
                raise GapError(
                    timeline=ref.timeline,
                    segno=ref.segno,
                    file_name=ref.file_name,
                    seg_start=ref.segment_start,
                    seg_end=ref.segment_end,
                    breakpoint_lsn=ref.start_lsn,
                )
            resolved = self.resolve_wal(seg)
            if ref.before_fork:
                role = "before-fork"
                note = f"分叉前：时间线 {ref.timeline} 的末段（只重放到分叉点前）"
            elif ref.after_fork:
                role = "after-fork"
                note = f"分叉后：时间线 {ref.timeline} 在分叉点后的续段"
            else:
                role = ""
                note = f"时间线 {ref.timeline} 的归档段"
            plan.steps.append(
                ChainStep(
                    kind="wal",
                    name=ref.file_name,
                    timeline=ref.timeline,
                    start_lsn=ref.start_lsn,
                    end_lsn=ref.end_lsn - 1,
                    segment_start=ref.segment_start,
                    segment_end=ref.segment_end,
                    files=[resolved],
                    note=note,
                    fork_role=role,
                )
            )

        if not plan.steps or not plan.verified:
            raise PlanningError("未能生成可校验的重放链")
        return plan


# ---------------------------------------------------------------------------
# Package building
# ---------------------------------------------------------------------------

def _write_tar_entry(tar: tarfile.TarFile, name: str, data: bytes | None,
                     path: Optional[Path] = None, mode: int = 0o640) -> None:
    info = tarfile.TarInfo(name)
    info.mtime = 0
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "postgres"
    info.gname = "postgres"
    if data is not None:
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    else:
        assert path is not None
        info.size = path.stat().st_size
        with open(path, "rb") as fh:
            tar.addfile(info, fh)


def build_package(catalog: Catalog, plan: RecoveryPlan, packages_dir: Path,
                  now: Optional[float] = None) -> PackageInfo:
    """Stream verified bytes into a tar.gz and return its metadata.

    Every file is re-hashed while being copied; a mismatch aborts the build so
    an unverified package is never published.
    """
    if not plan.verified:
        raise PitrError("重放链未全部通过校验，不能打包")
    created = time.time() if now is None else now
    packages_dir = Path(packages_dir)
    packages_dir.mkdir(parents=True, exist_ok=True)
    package_id = uuid.uuid4().hex
    filename = f"pitr-{plan.target_timeline}-{format_lsn(plan.target_lsn).replace('/', '-')}-{package_id[:8]}.tar.gz"
    final_path = packages_dir / filename
    tmp_path = packages_dir / f".{filename}.part"

    manifest = {
        "package_id": package_id,
        "created_at": created,
        "target_timeline": plan.target_timeline,
        "target_lsn": format_lsn(plan.target_lsn),
        "segment_size": plan.segment_size,
        "backup": {
            "id": plan.backup.backup_id,
            "timeline": plan.backup.timeline,
            "replay_start_lsn": format_lsn(plan.backup.start_lsn),
            "consistency_lsn": format_lsn(plan.backup.consistent_lsn),
        },
        "warnings": plan.warnings,
        "steps": [],
    }
    for index, step in enumerate(plan.steps, start=1):
        entry = {
            "order": index,
            "kind": step.kind,
            "name": step.name,
            "timeline": step.timeline,
            "start_lsn": format_lsn(step.start_lsn) if step.start_lsn is not None else None,
            "end_lsn": format_lsn(step.end_lsn) if step.end_lsn is not None else None,
            "note": step.note,
            "fork_role": step.fork_role,
            "files": [],
        }
        for f in step.files:
            entry["files"].append({
                "source": f.source_rel,
                "package_path": f.package_path,
                "sha256": f.sha256,
                "size": f.size,
                "copy": f.used_copy,
            })
        manifest["steps"].append(entry)

    readme = _readme_text(plan, manifest)
    recovery_signal = b""
    postgresql_auto = _postgresql_auto_conf(plan)

    digest = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, "wb") as raw:
            class HashingWriter(io.RawIOBase):
                def writable(self):
                    return True

                def write(self, b):
                    nonlocal size
                    digest.update(b)
                    size += len(b)
                    return raw.write(b)

            hasher = HashingWriter()
            with tarfile.open(fileobj=hasher, mode="w:gz", format=tarfile.PAX_FORMAT,
                              compresslevel=6) as tar:
                _write_tar_entry(tar, "README.txt", readme.encode("utf-8"))
                _write_tar_entry(tar, "recovery.signal", recovery_signal, mode=0o600)
                _write_tar_entry(tar, "postgresql.auto.conf",
                                 postgresql_auto.encode("utf-8"), mode=0o600)
                _write_tar_entry(tar, "pitr_plan.json",
                                 (json.dumps(manifest, indent=2, ensure_ascii=False)
                                  + "\n").encode("utf-8"), mode=0o644)
                for step in plan.steps:
                    if step.kind == "base":
                        _add_directory(tar, "basebackup/")
                    for f in step.files:
                        source = catalog._full_path(f.source_rel)
                        _verify_while_copying(source, f)
                        _write_tar_entry(tar, f.package_path, None, source)
            hasher.flush()
        os.replace(tmp_path, final_path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    info = PackageInfo(
        package_id=package_id,
        filename=filename,
        sha256=digest.hexdigest(),
        size=size,
        created_at=created,
        target_timeline=plan.target_timeline,
        target_lsn=format_lsn(plan.target_lsn),
        backup_id=plan.backup.backup_id,
        step_count=len(plan.steps),
    )
    meta = {
        "package_id": info.package_id,
        "filename": info.filename,
        "sha256": info.sha256,
        "size": info.size,
        "created_at": info.created_at,
        "target_timeline": info.target_timeline,
        "target_lsn": info.target_lsn,
        "backup_id": info.backup_id,
        "step_count": info.step_count,
        "status": "verified",
        "plan": manifest,
    }
    (packages_dir / (filename + PACKAGE_META_SUFFIX)).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return info


def _add_directory(tar: tarfile.TarFile, name: str) -> None:
    """Add an explicit directory entry so empty dirs survive extraction."""
    if not name.endswith("/"):
        name += "/"
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mtime = 0
    info.mode = 0o750
    info.uid = 0
    info.gid = 0
    info.uname = "postgres"
    info.gname = "postgres"
    tar.addfile(info)


def _verify_while_copying(source: Path, resolved: ResolvedFile) -> None:
    if not source.is_file():
        raise FileUnavailable(resolved.original_rel, resolved.sha256, ["打包时源文件丢失"])
    actual = sha256_file(source)
    if actual != resolved.sha256:
        raise FileUnavailable(
            resolved.original_rel, resolved.sha256,
            [f"打包时 SHA-256 变为 {actual}"],
        )


def _readme_text(plan: RecoveryPlan, manifest: dict) -> str:
    lines = [
        "PostgreSQL 指定时间点恢复（PITR）交付包",
        "=" * 44,
        f"目标时间线 : {plan.target_timeline}",
        f"目标 LSN    : {format_lsn(plan.target_lsn)}",
        f"全备        : {plan.backup.backup_id}（时间线 {plan.backup.timeline}）",
        f"重放起点    : {format_lsn(plan.backup.start_lsn)}",
        f"一致位点    : {format_lsn(plan.backup.consistent_lsn)}",
        "",
        "恢复步骤",
        "--------",
        "1. 解包后把 basebackup/ 下的内容复制到 $PGDATA（属主 postgres）。",
        "2. 把 wal/ 下的所有段放入归档目录或 pg_wal 可访问位置。",
        "3. 保留随包的 recovery.signal 与 postgresql.auto.conf（其中写入",
        "   restore_command、recovery_target_timeline、recovery_target_lsn）。",
        "4. 启动实例，PostgreSQL 将按下方顺序重放；到达目标 LSN 后自动停止。",
        "",
        "重放顺序",
        "--------",
    ]
    for step in manifest["steps"]:
        if step["kind"] == "base":
            lines.append(
                f"{step['order']:>2}. [全备] {step['name']} "
                f"(时间线 {step['timeline']}, {step['note']})"
            )
        else:
            lines.append(
                f"{step['order']:>2}. [WAL ] {step['name']} "
                f"({step['note']})"
            )
    if plan.copied_files:
        lines += ["", "使用了副本的文件", "----------------"]
        for f in plan.copied_files:
            lines.append(f"- {f.original_rel} ← {f.source_rel}")
    if plan.warnings:
        lines += ["", "注意", "----"]
        lines += [f"- {w}" for w in plan.warnings]
    return "\n".join(lines) + "\n"


def _postgresql_auto_conf(plan: RecoveryPlan) -> str:
    return "\n".join([
        "# generated by the PITR package builder",
        "restore_command = 'cp /var/lib/postgresql/archive/%f %p'",
        f"recovery_target_timeline = '{plan.target_timeline}'",
        f"recovery_target_lsn = '{format_lsn(plan.target_lsn)}'",
        "recovery_target_action = 'promote'",
        "",
    ])


# ---------------------------------------------------------------------------
# Package registry
# ---------------------------------------------------------------------------

class PackageStore:
    """Lists, loads and prunes built packages in one directory."""

    def __init__(self, packages_dir: Path, ttl_seconds: int = DEFAULT_PACKAGE_TTL):
        self.dir = Path(packages_dir)
        self.ttl = ttl_seconds
        self.dir.mkdir(parents=True, exist_ok=True)

    def list(self) -> List[PackageInfo]:
        out: List[PackageInfo] = []
        for meta_path in sorted(self.dir.glob(f"*{PACKAGE_META_SUFFIX}")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                info = PackageInfo(
                    package_id=meta["package_id"],
                    filename=meta["filename"],
                    sha256=meta["sha256"],
                    size=meta["size"],
                    created_at=meta["created_at"],
                    target_timeline=meta["target_timeline"],
                    target_lsn=meta["target_lsn"],
                    backup_id=meta["backup_id"],
                    step_count=meta["step_count"],
                    status=meta.get("status", "verified"),
                )
            except (OSError, KeyError, ValueError):
                continue
            if not (self.dir / info.filename).is_file():
                continue
            out.append(info)
        out.sort(key=lambda p: p.created_at, reverse=True)
        return out

    def get(self, package_id: str) -> Tuple[PackageInfo, Path, dict]:
        if not package_id or any(c not in "0123456789abcdef" for c in package_id):
            raise NotFound("非法包编号")
        for info in self.list():
            if info.package_id != package_id:
                continue
            path = self.dir / info.filename
            meta_path = self.dir / (info.filename + PACKAGE_META_SUFFIX)
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
            return info, path, meta
        raise NotFound(f"包 {package_id} 不存在")

    def prune(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        removed = 0
        for info in self.list():
            if now - info.created_at <= self.ttl:
                continue
            for p in (self.dir / info.filename,
                      self.dir / (info.filename + PACKAGE_META_SUFFIX)):
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        for part in self.dir.glob(".*.part"):
            try:
                if now - part.stat().st_mtime > 3600:
                    part.unlink()
            except OSError:
                pass
        return removed
