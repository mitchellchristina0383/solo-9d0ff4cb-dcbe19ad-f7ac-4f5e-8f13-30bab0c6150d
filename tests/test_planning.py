# -*- coding: utf-8 -*-
"""Planning tests: backup selection, intra-segment forks, gaps and reselection."""

from __future__ import annotations

import shutil

import pytest

from pitr.core import (
    AllChainsFailed,
    PlanningError,
    format_lsn,
    parse_lsn,
)
from pitr.store import Catalog


def plan(cat: Catalog, tli: str, lsn_text: str):
    return cat.plan(tli, parse_lsn(lsn_text))


def assert_contiguous(plan):
    """The plan must cover [replay_start, target) with no holes or overlaps."""
    cursor = plan.backup.start_lsn
    for step in plan.wal_steps:
        assert step.start_lsn == cursor, (
            f"{step.name} starts {format_lsn(step.start_lsn)}, expected "
            f"{format_lsn(cursor)}"
        )
        assert step.end_lsn >= step.start_lsn
        cursor = step.end_lsn + 1
    assert cursor >= plan.target_lsn, (
        f"chain ends at {format_lsn(cursor - 1)}, target "
        f"{format_lsn(plan.target_lsn)}"
    )


# ---------------------------------------------------------------------------
# Backup selection
# ---------------------------------------------------------------------------

def test_picks_closest_consistency_point_not_after_target(catalog):
    # Target 0/560000 lies on timeline 2.  base_20251123_0100 (0/540000) is the
    # closest usable base, but its pg_control is corrupt with no duplicate, so
    # the next-closest usable backup must be chosen.
    p = plan(catalog, "00000002", "0/560000")
    assert p.backup.backup_id == "base_20251122_0100"
    assert p.backup.consistent_lsn == parse_lsn("0/3C0000")
    assert p.verified
    assert_contiguous(p)


def test_never_picks_backup_after_target(catalog):
    # 0/3A0000 sits between base_20251122_0100 (0/3C0000, too late) and
    # base_20251121_0100 (0/2C0000, on timeline 1 whose WAL reaches 0/300000).
    p = plan(catalog, "00000002", "0/3A0000")
    assert p.backup.backup_id == "base_20251120_0100"
    assert_contiguous(p)


def test_target_on_root_timeline(catalog):
    p = plan(catalog, "00000001", "0/200000")
    assert p.backup.backup_id == "base_20251120_0100"
    assert [s.name for s in p.wal_steps] == [
        "000000010000000000000000",
        "000000010000000000000001",
    ]
    assert_contiguous(p)


def test_backup_selection_is_deterministic(catalog):
    p1 = plan(catalog, "00000002", "0/560000")
    cat2 = Catalog.load(catalog.data_root)
    p2 = cat2.plan("00000002", parse_lsn("0/560000"))
    assert p1.backup.backup_id == p2.backup.backup_id
    assert [s.name for s in p1.steps] == [s.name for s in p2.steps]


# ---------------------------------------------------------------------------
# Intra-segment forks
# ---------------------------------------------------------------------------

def test_fork_inside_segment_2_3_uses_both_timeline_files(catalog):
    # 2->3 switchpoint 0/580000 is inside 1 MiB segment 5.
    p = plan(catalog, "00000003", "0/600000")
    names = [(s.timeline, s.name) for s in p.wal_steps]
    assert names == [
        ("00000002", "000000020000000000000003"),
        ("00000002", "000000020000000000000004"),
        ("00000002", "000000020000000000000005"),
        ("00000003", "000000030000000000000005"),
    ]
    before, after = p.wal_steps[-2], p.wal_steps[-1]
    assert before.fork_role == "before-fork"
    assert after.fork_role == "after-fork"
    assert before.end_lsn == parse_lsn("0/57FFFF")
    assert after.start_lsn == parse_lsn("0/580000")
    assert before.segment_start == after.segment_start  # same segment slot
    assert_contiguous(p)
    assert p.verified


def test_fork_on_segment_boundary_switches_file_cleanly(catalog):
    # The 1->2 switchpoint 0/300000 coincides with segment 3's start, so the
    # last parent file is complete and the child's segment 3 is the first after
    # the fork; no file is truncated and no timeline is duplicated.
    p = plan(catalog, "00000002", "0/320000")
    assert p.backup.backup_id == "base_20251120_0100"
    roles = [(s.timeline, s.name, s.fork_role) for s in p.wal_steps]
    assert roles[-2:] == [
        ("00000001", "000000010000000000000002", ""),
        ("00000002", "000000020000000000000003", ""),
    ]
    assert p.wal_steps[-2].end_lsn == parse_lsn("0/2FFFFF")
    assert p.wal_steps[-1].start_lsn == parse_lsn("0/300000")
    assert_contiguous(p)


def test_target_exactly_on_segment_boundary_fork_point(catalog):
    # A target exactly at the 0/300000 boundary replays only up to 0/2FFFFF,
    # so timeline 2's segment 3 is not needed at all.
    p = plan(catalog, "00000002", "0/300000")
    assert [s.name for s in p.wal_steps] == [
        "000000010000000000000000",
        "000000010000000000000001",
        "000000010000000000000002",
    ]
    assert p.wal_steps[-1].end_lsn == parse_lsn("0/2FFFFF")
    assert_contiguous(p)


def test_target_exactly_on_fork_switchpoint_changes_timeline(catalog):
    # On the switchpoint means the target is the first byte of the child
    # timeline; everything before it is replayed from the parent.
    p = plan(catalog, "00000003", "0/580000")
    assert p.wal_steps[-1].timeline == "00000002"
    assert p.wal_steps[-1].end_lsn == parse_lsn("0/57FFFF")
    assert_contiguous(p)
    # Asking for that LSN on the parent timeline is rejected and points the
    # operator at the child.
    with pytest.raises(PlanningError, match="分叉点"):
        plan(catalog, "00000002", "0/580000")


def test_target_on_segment_boundary_does_not_need_next_file(catalog):
    # Target 0/3BFF00 sits inside timeline 2's segment 3, so segment 4 is not
    # pulled in just because the target is near the segment end.
    p = plan(catalog, "00000002", "0/3BFF00")
    assert p.wal_steps[-1].name == "000000020000000000000003"
    assert p.wal_steps[-1].end_lsn == parse_lsn("0/3BFEFF")
    assert_contiguous(p)


# ---------------------------------------------------------------------------
# Gaps and reselection
# ---------------------------------------------------------------------------

def test_gap_reports_explicit_breakpoint(catalog):
    # Timeline 3 segment 7 is missing from the catalogue.
    with pytest.raises(AllChainsFailed) as exc:
        plan(catalog, "00000003", "0/800000")
    gaps = [a for a in exc.value.attempts if a.kind == "gap"]
    assert gaps, "at least one attempt must be classified as a gap"
    gap = gaps[0].gap
    assert gap.file_name == "000000030000000000000007"
    assert gap.breakpoint_lsn == parse_lsn("0/700000")
    assert "断点" in str(gap)
    assert "000000030000000000000007" in str(gap)


def test_corrupt_backup_without_copy_causes_reselection(catalog):
    # 0/560000: base_20251123_0100 is closest but its pg_control is corrupt
    # with no duplicate; the plan must fall back to base_20251122_0100 and note
    # the abandoned backup.
    p = plan(catalog, "00000002", "0/560000")
    assert p.backup.backup_id == "base_20251122_0100"
    assert any("base_20251123_0100" in w and "已放弃" in w for w in p.warnings)
    corrupt = [a for a in p.attempts if a.kind == "corrupt"]
    assert corrupt and corrupt[0].backup.backup_id == "base_20251123_0100"
    assert "global/pg_control" in corrupt[0].files[0]


def test_corrupt_wal_uses_duplicate_copy(catalog):
    # Timeline 3 segment 5 primary is corrupted; the plan must use its copy.
    p = plan(catalog, "00000003", "0/600000")
    step = [s for s in p.wal_steps if s.name == "000000030000000000000005"][0]
    assert step.verified
    assert step.used_copies
    assert step.used_copies[0].source_rel.startswith("copies/")
    assert step.used_copies[0].original_rel == "wal/000000030000000000000005"


def test_corrupt_base_file_with_copy_is_recovered(catalog):
    # base_20251122_0100's global/pg_control is corrupt but has a copy.
    p = plan(catalog, "00000002", "0/560000")
    base = p.steps[0]
    used = base.used_copies
    assert used and used[0].package_path == "basebackup/global/pg_control"
    assert used[0].source_rel.startswith("copies/")


def test_no_candidate_at_all_raises_with_attempts(catalog, sample_dir):
    # Delete every segment of timeline 3 so no backup can serve 0/700000.
    broken = sample_dir.parent / "broken-no-wal"
    if broken.exists():
        shutil.rmtree(broken)
    shutil.copytree(sample_dir, broken)
    wal_path = broken / "wal_manifest.json"
    import json
    raw = json.loads(wal_path.read_text())
    raw["segments"] = [s for s in raw["segments"] if s["timeline"] != "00000003"]
    wal_path.write_text(json.dumps(raw))
    cat = Catalog.load(broken)
    with pytest.raises(AllChainsFailed) as exc:
        cat.plan("00000003", parse_lsn("0/600000"))
    assert exc.value.attempts
    assert all(a.kind in ("gap", "corrupt") for a in exc.value.attempts)
