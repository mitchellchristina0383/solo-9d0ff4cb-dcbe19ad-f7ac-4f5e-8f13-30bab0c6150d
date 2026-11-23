# -*- coding: utf-8 -*-
"""Domain layer for the PITR (point-in-time recovery) file builder.

Pure logic only: no Flask, no filesystem writes.  The module parses the sample
manifests into typed models, models timeline history, chooses a base backup and
assembles the ordered WAL segment chain, including intra-segment timeline
forks.  Reading bytes and verifying SHA-256 lives in :mod:`pitr.store`.

LSNs use PostgreSQL's textual ``X/Y`` form (both parts hexadecimal) and are
normalised to integers so comparisons stay numeric.

Target LSN semantics
--------------------
A target is the recovery point: everything *before* it is replayed, so the WAL
span required is ``[replay_start, target_lsn)`` and the last segment needed is
the one containing ``target_lsn - 1``.  A target exactly on a segment boundary
therefore does not require the next segment's file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

LSN_RE = re.compile(r"^[0-9A-Fa-f]{1,8}/[0-9A-Fa-f]{1,8}$")
WAL_NAME_RE = re.compile(r"^([0-9A-F]{8})([0-9A-F]{8})([0-9A-F]{8})$")
TIMELINE_RE = re.compile(r"^[0-9A-F]{8}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

MIN_SEGMENT_BYTES = 1024 * 1024
MAX_SEGMENT_BYTES = 1024 * 1024 * 1024


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PitrError(Exception):
    """Base class for problems surfaced to the operator."""


class ManifestError(PitrError):
    """A manifest is malformed or internally inconsistent."""


class PlanningError(PitrError):
    """The requested target cannot be planned at all."""


class GapError(PlanningError):
    """A WAL segment required by the chain is absent from the catalogue."""

    def __init__(self, timeline: str, segno: int, file_name: str,
                 seg_start: int, seg_end: int, breakpoint_lsn: int):
        self.timeline = timeline
        self.segno = segno
        self.file_name = file_name
        self.seg_start = seg_start
        self.seg_end = seg_end              # exclusive
        self.breakpoint_lsn = breakpoint_lsn
        super().__init__(
            f"断点：时间线 {timeline} 缺少 WAL 段 {file_name}"
            f"（覆盖 {format_lsn(seg_start)}–{format_lsn(seg_end - 1)}），"
            f"无法从 {format_lsn(breakpoint_lsn)} 继续重放"
        )


class FileUnavailable(PitrError):
    """A payload file and every known copy failed verification."""

    def __init__(self, path: str, expected: str, reasons: Sequence[str]):
        self.path = path
        self.expected = expected
        self.reasons = list(reasons)
        super().__init__(
            f"文件 {path} 无法通过 SHA-256 校验（期望 {expected}）："
            + "；".join(reasons)
        )


class ChainFailure(PitrError):
    """A single candidate backup could not produce a complete valid chain."""

    def __init__(self, backup: "BaseBackup", kind: str, message: str,
                 gap: Optional[GapError] = None, files: Sequence[str] = ()):
        super().__init__(message)
        self.backup = backup
        self.kind = kind                # "gap" | "corrupt" | "error"
        self.message = message
        self.gap = gap
        self.files = tuple(files)


class AllChainsFailed(PlanningError):
    """Every candidate backup was tried and each one failed."""

    def __init__(self, message: str, attempts: Sequence[ChainFailure]):
        super().__init__(message)
        self.attempts = list(attempts)


# ---------------------------------------------------------------------------
# LSN / WAL filename helpers
# ---------------------------------------------------------------------------

def parse_lsn(text: str) -> int:
    if not isinstance(text, str) or not LSN_RE.match(text):
        raise ManifestError(f"无效的 LSN：{text!r}（应形如 0/1A0000）")
    hi, lo = text.split("/")
    return (int(hi, 16) << 32) | int(lo, 16)


def format_lsn(value: int) -> str:
    if value < 0:
        raise ValueError("LSN cannot be negative")
    return f"{value >> 32:X}/{(value & 0xFFFFFFFF):X}"


def format_range(start: int, end_exclusive: int) -> str:
    """Render an inclusive display range for the half-open span."""
    if end_exclusive <= start:
        return f"{format_lsn(start)}（空）"
    return f"{format_lsn(start)} – {format_lsn(end_exclusive - 1)}"


def validate_segment_size(size) -> int:
    if not isinstance(size, int) or isinstance(size, bool):
        raise ManifestError("segment_size 必须是整数")
    if size < MIN_SEGMENT_BYTES or size > MAX_SEGMENT_BYTES:
        raise ManifestError(
            f"segment_size 必须在 {MIN_SEGMENT_BYTES}–{MAX_SEGMENT_BYTES} 字节之间"
        )
    if size & (size - 1):
        raise ManifestError("segment_size 必须是 2 的幂")
    return size


def segments_per_log(segment_size: int) -> int:
    return 0x100000000 // segment_size


def wal_name(timeline: str, segno: int, segment_size: int) -> str:
    if segno < 0:
        raise ValueError("segment number cannot be negative")
    per_log = segments_per_log(segment_size)
    log_id, seg = divmod(segno, per_log)
    return f"{timeline}{log_id:08X}{seg:08X}"


def parse_wal_name(name: str, segment_size: int) -> Tuple[str, int]:
    if not isinstance(name, str):
        raise ManifestError(f"无效的 WAL 文件名：{name!r}")
    m = WAL_NAME_RE.match(name)
    if not m:
        raise ManifestError(f"无效的 WAL 文件名：{name!r}（需 24 位大写十六进制）")
    tli, log_id, seg = m.group(1), int(m.group(2), 16), int(m.group(3), 16)
    per_log = segments_per_log(segment_size)
    if seg >= per_log:
        raise ManifestError(
            f"WAL 文件 {name!r} 的段号 {seg} 超出 segment_size={segment_size} 的范围"
        )
    return tli, log_id * per_log + seg


def segno_start(segno: int, segment_size: int) -> int:
    return segno * segment_size


def segno_for_lsn(lsn: int, segment_size: int) -> int:
    if lsn < 0:
        raise ValueError("LSN cannot be negative")
    return lsn // segment_size


def safe_rel_path(rel: str, where: str) -> str:
    """Validate a POSIX relative path read from a manifest."""
    from pathlib import PurePosixPath

    if not isinstance(rel, str) or not rel:
        raise ManifestError(f"{where}：路径必须是非空字符串")
    if "\\" in rel:
        raise ManifestError(f"{where}：路径不允许反斜杠")
    p = PurePosixPath(rel)
    if p.is_absolute() or any(part in ("", ".", "..") for part in p.parts):
        raise ManifestError(f"{where}：不安全的相对路径 {rel!r}")
    return rel


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Timeline:
    timeline: str
    parent: Optional[str]
    switchpoint: Optional[int]  # LSN at which this timeline diverged from parent

    @property
    def timeline_int(self) -> int:
        return int(self.timeline, 16)

    @property
    def start_lsn(self) -> int:
        return self.switchpoint if self.switchpoint is not None else 0

    @property
    def start_text(self) -> str:
        return format_lsn(self.start_lsn)


@dataclass(frozen=True)
class FileEntry:
    """One payload file of a base backup, relative to ``full/<backup_id>/``."""
    path: str
    sha256: str
    size: Optional[int] = None
    copies: Tuple[str, ...] = ()   # alternative copies, relative to data root


@dataclass(frozen=True)
class BaseBackup:
    backup_id: str
    timeline: str
    start_lsn: int            # WAL replay start (重放起点)
    consistent_lsn: int       # consistency point (一致位点)
    manifest: str = ""
    files: Tuple[FileEntry, ...] = ()
    label: str = ""

    @property
    def timeline_int(self) -> int:
        return int(self.timeline, 16)

    @property
    def dir_rel(self) -> str:
        return f"full/{self.backup_id}"


@dataclass(frozen=True)
class WalSegment:
    file_name: str
    timeline: str
    segno: int
    sha256: str
    path: str = ""
    size: Optional[int] = None
    copies: Tuple[str, ...] = ()   # relative to data root

    @property
    def timeline_int(self) -> int:
        return int(self.timeline, 16)


@dataclass(frozen=True)
class ResolvedFile:
    """A file chosen (and verified) for packaging."""
    source_rel: str      # actual source path relative to data root
    package_path: str    # destination path inside the package
    sha256: str
    size: int
    original_rel: str
    used_copy: bool


@dataclass
class ChainStep:
    """One replay item shown to the operator."""
    kind: str                  # "base" | "wal"
    name: str                  # backup id or WAL filename
    timeline: str
    start_lsn: Optional[int] = None
    end_lsn: Optional[int] = None      # inclusive display end
    segment_start: Optional[int] = None
    segment_end: Optional[int] = None  # exclusive
    files: List[ResolvedFile] = field(default_factory=list)
    note: str = ""
    fork_role: str = ""        # "" | "before-fork" | "after-fork"
    verify_error: Optional[str] = None

    @property
    def verified(self) -> bool:
        return self.verify_error is None

    @property
    def used_copies(self) -> List[ResolvedFile]:
        return [f for f in self.files if f.used_copy]

    @property
    def kind_label(self) -> str:
        return "全备" if self.kind == "base" else "WAL"

    @property
    def range_text(self) -> str:
        if self.start_lsn is None or self.end_lsn is None:
            return ""
        return format_range(self.start_lsn, self.end_lsn + 1)


@dataclass
class RecoveryPlan:
    target_timeline: str
    target_lsn: int
    segment_size: int
    backup: BaseBackup
    steps: List[ChainStep] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    skipped: List[Tuple[BaseBackup, str]] = field(default_factory=list)
    attempts: List[ChainFailure] = field(default_factory=list)

    @property
    def wal_steps(self) -> List[ChainStep]:
        return [s for s in self.steps if s.kind == "wal"]

    @property
    def all_files(self) -> List[ResolvedFile]:
        return [f for s in self.steps for f in s.files]

    @property
    def copied_files(self) -> List[ResolvedFile]:
        return [f for s in self.steps for f in s.files if f.used_copy]

    @property
    def verified(self) -> bool:
        return bool(self.steps) and all(s.verified for s in self.steps)

    def forks(self) -> List[Tuple[int, ChainStep, ChainStep]]:
        """Adjacent WAL steps where the replay crosses to another timeline."""
        out = []
        wals = self.wal_steps
        for i in range(len(wals) - 1):
            if wals[i].timeline != wals[i + 1].timeline:
                out.append((i, wals[i], wals[i + 1]))
        return out


@dataclass(frozen=True)
class PackageInfo:
    package_id: str
    filename: str
    sha256: str
    size: int
    created_at: float
    target_timeline: str
    target_lsn: str
    backup_id: str
    step_count: int
    status: str = "verified"


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------

def _require(obj: dict, key: str, where: str):
    if not isinstance(obj, dict) or key not in obj:
        raise ManifestError(f"{where}：缺少必需字段 {key!r}")
    return obj[key]


def _as_lsn(value, where: str) -> int:
    try:
        return parse_lsn(value)
    except ManifestError as exc:
        raise ManifestError(f"{where}：{exc}") from exc


def _hex_digest(value, where: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.match(value):
        raise ManifestError(f"{where}：sha256 必须是 64 位十六进制")
    return value.lower()


def _optional_size(item: dict, where: str) -> Optional[int]:
    size = item.get("size")
    if size is None:
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ManifestError(f"{where}：size 必须是非负整数")
    return size


def _str_list(value, where: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ManifestError(f"{where}：必须是字符串列表")
    return tuple(safe_rel_path(v, where) for v in value)


def parse_timelines(raw: dict) -> Tuple[int, Dict[str, Timeline]]:
    if not isinstance(raw, dict):
        raise ManifestError("时间线清单必须是 JSON 对象")
    segment_size = validate_segment_size(_require(raw, "segment_size", "timelines"))
    items = _require(raw, "timelines", "timelines")
    if not isinstance(items, list) or not items:
        raise ManifestError("timelines：必须是非空列表")
    out: Dict[str, Timeline] = {}
    for i, item in enumerate(items):
        where = f"timelines[{i}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{where}：必须是对象")
        tli = str(_require(item, "timeline", where)).upper()
        if not TIMELINE_RE.match(tli):
            raise ManifestError(f"{where}：时间线 id 非法 {tli!r}")
        if tli in out:
            raise ManifestError(f"{where}：时间线 {tli} 重复")
        parent = item.get("parent")
        if parent is not None:
            parent = str(parent).upper()
            if not TIMELINE_RE.match(parent):
                raise ManifestError(f"{where}：父时间线非法 {parent!r}")
        sp_raw = item.get("switchpoint_lsn")
        sp = None if sp_raw is None else _as_lsn(sp_raw, f"{where}.switchpoint_lsn")
        if parent is None and sp is not None:
            raise ManifestError(f"{where}：根时间线不应有 switchpoint")
        if parent is not None and sp is None:
            raise ManifestError(f"{where}：非根时间线必须有 switchpoint")
        out[tli] = Timeline(tli, parent, sp)

    roots = [t for t in out.values() if t.parent is None]
    if len(roots) != 1:
        raise ManifestError(f"时间线历史必须恰好有一个根，实际 {len(roots)} 个")
    for t in out.values():
        if t.parent is not None and t.parent not in out:
            raise ManifestError(f"时间线 {t.timeline}：父时间线 {t.parent} 不在历史中")
        timeline_ancestry(t.timeline, out)  # cycle check
    for t in out.values():
        parent = out[t.parent] if t.parent is not None else None
        if parent is not None and parent.switchpoint is not None:
            if t.switchpoint <= parent.switchpoint:
                raise ManifestError(
                    f"时间线 {t.timeline}：switchpoint {format_lsn(t.switchpoint)} "
                    f"必须晚于父时间线 {t.parent} 的 {format_lsn(parent.switchpoint)}"
                )
    return segment_size, out


def timeline_ancestry(tli: str, timelines: Dict[str, Timeline]) -> List[str]:
    """Return ``[tli, parent, ..., root]``."""
    chain: List[str] = []
    seen = set()
    cur: Optional[str] = tli
    while cur is not None:
        if cur in seen:
            raise ManifestError(f"时间线历史存在环：{cur}")
        seen.add(cur)
        if cur not in timelines:
            raise ManifestError(f"时间线 {cur} 不在历史中")
        chain.append(cur)
        cur = timelines[cur].parent
    return chain


def timeline_children(tli: str, timelines: Dict[str, Timeline]) -> List[Timeline]:
    return sorted(
        (t for t in timelines.values() if t.parent == tli),
        key=lambda t: t.timeline_int,
    )


def timeline_path(backup: BaseBackup, target_timeline: str,
                  timelines: Dict[str, Timeline]) -> List[str]:
    """Timelines from the backup's timeline to the target timeline, root first."""
    target_chain = timeline_ancestry(target_timeline, timelines)
    if backup.timeline not in target_chain:
        raise PlanningError(
            f"全备 {backup.backup_id} 所在时间线 {backup.timeline} 不是目标时间线 "
            f"{target_timeline} 的祖先"
        )
    return list(reversed(target_chain[: target_chain.index(backup.timeline) + 1]))


def validate_target(target_timeline: str, target_lsn: int,
                    timelines: Dict[str, Timeline]) -> None:
    if not TIMELINE_RE.match(target_timeline or ""):
        raise PlanningError(f"目标时间线格式非法 {target_timeline!r}")
    if target_timeline not in timelines:
        raise PlanningError(f"未知目标时间线 {target_timeline}")
    start = timelines[target_timeline].start_lsn
    if target_lsn < start:
        raise PlanningError(
            f"目标 LSN {format_lsn(target_lsn)} 早于时间线 {target_timeline} 的起点 "
            f"{format_lsn(start)}"
        )
    if target_lsn == 0:
        raise PlanningError("目标 LSN 必须大于 0")
    for child in timeline_children(target_timeline, timelines):
        if child.switchpoint is not None and target_lsn >= child.switchpoint:
            raise PlanningError(
                f"目标 LSN {format_lsn(target_lsn)} 已到达或越过时间线 "
                f"{target_timeline}→{child.timeline} 的分叉点 "
                f"{format_lsn(child.switchpoint)}，应改用时间线 {child.timeline}"
            )


def parse_backups(raw: dict, timelines: Dict[str, Timeline]) -> List[BaseBackup]:
    if not isinstance(raw, dict):
        raise ManifestError("全备清单必须是 JSON 对象")
    items = _require(raw, "backups", "backups")
    if not isinstance(items, list) or not items:
        raise ManifestError("backups：必须是非空列表")
    out: List[BaseBackup] = []
    seen = set()
    for i, item in enumerate(items):
        where = f"backups[{i}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{where}：必须是对象")
        bid = str(_require(item, "id", where))
        if not bid or "/" in bid or "\\" in bid or bid in (".", ".."):
            raise ManifestError(f"{where}：全备 id 不安全 {bid!r}")
        if bid in seen:
            raise ManifestError(f"{where}：全备 id {bid!r} 重复")
        seen.add(bid)
        tli = str(_require(item, "timeline", where)).upper()
        if tli not in timelines:
            raise ManifestError(f"{where}：未知时间线 {tli!r}")
        start = _as_lsn(_require(item, "replay_start_lsn", where), f"{where}.replay_start_lsn")
        consistent = _as_lsn(
            _require(item, "consistency_lsn", where), f"{where}.consistency_lsn"
        )
        if consistent < start:
            raise ManifestError(
                f"{where}：一致位点 {format_lsn(consistent)} 早于重放起点 "
                f"{format_lsn(start)}"
            )
        if consistent < timelines[tli].start_lsn:
            raise ManifestError(
                f"{where}：一致位点 {format_lsn(consistent)} 早于时间线 "
                f"{tli} 的起点 {format_lsn(timelines[tli].start_lsn)}"
            )
        manifest = safe_rel_path(str(_require(item, "manifest", where)), f"{where}.manifest")
        out.append(
            BaseBackup(
                backup_id=bid,
                timeline=tli,
                start_lsn=start,
                consistent_lsn=consistent,
                manifest=manifest,
                label=str(item.get("label", "")),
            )
        )
    return out


def parse_backup_manifest(raw: dict, backup: BaseBackup) -> BaseBackup:
    """Parse a per-backup payload manifest and return the updated backup."""
    if not isinstance(raw, dict):
        raise ManifestError(f"{backup.backup_id} 的文件清单必须是 JSON 对象")
    bid = raw.get("backup_id")
    if bid is not None and str(bid) != backup.backup_id:
        raise ManifestError(f"{backup.backup_id} 的文件清单 backup_id={bid!r} 不匹配")
    items = _require(raw, "files", f"manifest[{backup.backup_id}]")
    if not isinstance(items, list) or not items:
        raise ManifestError(f"{backup.backup_id}：文件清单必须是非空列表")
    files: List[FileEntry] = []
    seen = set()
    for i, item in enumerate(items):
        where = f"manifest[{backup.backup_id}].files[{i}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{where}：必须是对象")
        path = safe_rel_path(str(_require(item, "path", where)), f"{where}.path")
        if path in seen:
            raise ManifestError(f"{where}：路径 {path} 重复")
        seen.add(path)
        files.append(
            FileEntry(
                path=path,
                sha256=_hex_digest(_require(item, "sha256", where), f"{where}.sha256"),
                size=_optional_size(item, where),
                copies=_str_list(item.get("copies"), f"{where}.copies"),
            )
        )
    return replace(backup, files=tuple(files))


def parse_wal(raw: dict, segment_size: int,
              timelines: Dict[str, Timeline]) -> List[WalSegment]:
    if not isinstance(raw, dict):
        raise ManifestError("WAL 清单必须是 JSON 对象")
    items = _require(raw, "segments", "segments")
    if not isinstance(items, list) or not items:
        raise ManifestError("segments：必须是非空列表")
    out: List[WalSegment] = []
    seen = set()
    for i, item in enumerate(items):
        where = f"segments[{i}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{where}：必须是对象")
        name = str(_require(item, "file_name", where))
        tli_from_name, segno = parse_wal_name(name, segment_size)
        tli = str(item.get("timeline", tli_from_name)).upper()
        if tli != tli_from_name:
            raise ManifestError(f"{where}：时间线 {tli!r} 与文件名 {name!r} 不一致")
        if tli not in timelines:
            raise ManifestError(f"{where}：未知时间线 {tli!r}")
        if name in seen:
            raise ManifestError(f"{where}：WAL 文件 {name!r} 重复")
        seen.add(name)
        path = safe_rel_path(str(item.get("path", f"wal/{name}")), f"{where}.path")
        out.append(
            WalSegment(
                file_name=name,
                timeline=tli,
                segno=segno,
                sha256=_hex_digest(_require(item, "sha256", where), f"{where}.sha256"),
                path=path,
                size=_optional_size(item, where),
                copies=_str_list(item.get("copies"), f"{where}.copies"),
            )
        )
    out.sort(key=lambda s: (s.segno, s.timeline_int))
    return out


# ---------------------------------------------------------------------------
# Backup selection
# ---------------------------------------------------------------------------

def backup_unusable_reason(backup: BaseBackup, target_lsn: int,
                           target_timeline: str,
                           timelines: Dict[str, Timeline]) -> Optional[str]:
    """Return a human-readable reason, or ``None`` when the backup is usable."""
    if backup.consistent_lsn > target_lsn:
        return (
            f"一致位点 {format_lsn(backup.consistent_lsn)} 晚于目标 "
            f"{format_lsn(target_lsn)}"
        )
    if backup.timeline == target_timeline:
        # A backup on the target timeline is always on the branch.
        return None
    try:
        path = timeline_path(backup, target_timeline, timelines)
    except PlanningError as exc:
        return str(exc)
    own = path.index(backup.timeline)
    if own + 1 < len(path):
        fork_tli = path[own + 1]
        fork = timelines[fork_tli].switchpoint
        if backup.consistent_lsn >= fork:
            return (
                f"一致位点 {format_lsn(backup.consistent_lsn)} 已越过 "
                f"{backup.timeline}→{fork_tli} 分叉点 {format_lsn(fork)}，"
                f"不能沿该分支重放"
            )
        if backup.start_lsn >= fork:
            return (
                f"重放起点 {format_lsn(backup.start_lsn)} 已越过 "
                f"{backup.timeline}→{fork_tli} 分叉点 {format_lsn(fork)}"
            )
    return None


def candidate_backups(backups: Sequence[BaseBackup], target_lsn: int,
                      target_timeline: str,
                      timelines: Dict[str, Timeline]) -> List[BaseBackup]:
    """Usable backups, closest consistency point to the target first.

    "最接近且不晚于目标" is honoured by filtering every backup whose
    consistency point is later than the target, then sorting descending.
    """
    usable = [
        b for b in backups
        if backup_unusable_reason(b, target_lsn, target_timeline, timelines) is None
    ]
    usable.sort(key=lambda b: (b.consistent_lsn, b.start_lsn, b.backup_id), reverse=True)
    return usable


# ---------------------------------------------------------------------------
# Segment chain
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SegmentRef:
    """One WAL segment slot in the replay chain."""
    timeline: str
    segno: int
    file_name: str
    segment_start: int
    segment_end: int       # exclusive
    start_lsn: int         # effective start (inclusive)
    end_lsn: int           # effective end (exclusive)
    before_fork: bool = False   # parent-side file of an intra-segment fork
    after_fork: bool = False    # child-side file of an intra-segment fork

    @property
    def range_text(self) -> str:
        return format_range(self.start_lsn, self.end_lsn)


def build_segment_chain(backup: BaseBackup, target_lsn: int, target_timeline: str,
                        timelines: Dict[str, Timeline], segment_size: int,
                        available: Iterable[Tuple[str, int]]) -> List[SegmentRef]:
    """Assemble ordered WAL segment references from the backup to ``target_lsn``.

    ``available`` is an iterable of ``(timeline, segno)`` pairs present in the
    catalogue.  A missing pair raises :class:`GapError` naming the break.
    """
    validate_target(target_timeline, target_lsn, timelines)
    if backup.consistent_lsn > target_lsn:
        raise PlanningError(
            f"全备 {backup.backup_id} 的一致位点 "
            f"{format_lsn(backup.consistent_lsn)} 晚于目标 {format_lsn(target_lsn)}"
        )
    path = timeline_path(backup, target_timeline, timelines)
    have = set(available)

    if target_lsn <= backup.start_lsn:
        return []

    cursor = backup.start_lsn
    refs: List[SegmentRef] = []

    for pos, tli in enumerate(path):
        nxt = path[pos + 1] if pos + 1 < len(path) else None
        fork = timelines[nxt].switchpoint if nxt is not None else None
        # Replay on this timeline stops before its child's switchpoint, and
        # never beyond the target.
        limit = target_lsn if fork is None else min(fork, target_lsn)

        segno = segno_for_lsn(cursor, segment_size)
        while cursor < limit:
            seg_begin = segno_start(segno, segment_size)
            seg_finish = seg_begin + segment_size
            if seg_begin >= limit:
                break
            name = wal_name(tli, segno, segment_size)
            if (tli, segno) not in have:
                raise GapError(
                    timeline=tli,
                    segno=segno,
                    file_name=name,
                    seg_start=seg_begin,
                    seg_end=seg_finish,
                    breakpoint_lsn=cursor,
                )
            eff_end = min(seg_finish, limit)
            refs.append(
                SegmentRef(
                    timeline=tli,
                    segno=segno,
                    file_name=name,
                    segment_start=seg_begin,
                    segment_end=seg_finish,
                    start_lsn=cursor,
                    end_lsn=eff_end,
                    before_fork=(
                        fork is not None and limit == fork and seg_finish > fork
                    ),
                    after_fork=(
                        timelines[tli].switchpoint is not None
                        and seg_begin < timelines[tli].switchpoint < seg_finish
                    ),
                )
            )
            cursor = eff_end
            segno += 1

        if cursor >= target_lsn or nxt is None:
            break
        # Cross to the child timeline.  If the fork falls inside the segment
        # just emitted, the next file is the child's copy of the same segno.
        cursor = max(cursor, fork)
        if cursor >= target_lsn:
            break

    if cursor < target_lsn:
        raise GapError(
            timeline=path[-1],
            segno=segno_for_lsn(cursor, segment_size),
            file_name=wal_name(path[-1], segno_for_lsn(cursor, segment_size), segment_size),
            seg_start=segno_start(segno_for_lsn(cursor, segment_size), segment_size),
            seg_end=segno_start(segno_for_lsn(cursor, segment_size), segment_size)
            + segment_size,
            breakpoint_lsn=cursor,
        )
    return refs
