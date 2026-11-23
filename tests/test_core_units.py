# -*- coding: utf-8 -*-
"""Unit tests for LSN/WAL-name helpers and manifest parsing."""

from __future__ import annotations

import pytest

from pitr import core
from pitr.core import (
    ManifestError,
    format_lsn,
    parse_lsn,
    parse_timelines,
    parse_wal_name,
    wal_name,
)

SEG = 1024 * 1024


@pytest.mark.parametrize("text,value", [
    ("0/0", 0),
    ("0/1", 1),
    ("0/FFFFF", 0xFFFFF),
    ("1/0", 0x100000000),
    ("A/BCDEF", (10 << 32) | 0xBCDEF),
])
def test_lsn_round_trip(text, value):
    assert parse_lsn(text) == value
    assert parse_lsn(format_lsn(value)) == value


@pytest.mark.parametrize("bad", ["", "0", "0/", "/0", "0/G", "0x10/0", None, 12,
                                 "0/1/2", " 0/1"])
def test_lsn_rejects_garbage(bad):
    with pytest.raises(ManifestError):
        parse_lsn(bad)


def test_wal_name_matches_postgres_layout():
    assert wal_name("00000003", 5, SEG) == "000000030000000000000005"
    tli, segno = parse_wal_name("000000030000000000000005", SEG)
    assert (tli, segno) == ("00000003", 5)


def test_wal_name_crosses_log_boundary():
    # With 1 MiB segments there are 4096 segments per 4 GiB log file.
    name = wal_name("00000002", 4096, SEG)
    assert name == "000000020000000100000000"
    assert parse_wal_name(name, SEG) == ("00000002", 4096)


@pytest.mark.parametrize("bad", ["00000003000000000000005", "0000000300000000000000050",
                                 "00000003000000000000000g", "000000030000000000001000"])
def test_wal_name_rejects_bad_names(bad):
    with pytest.raises(ManifestError):
        parse_wal_name(bad, SEG)


def test_timeline_validation(tmp_path):
    raw = {
        "segment_size": SEG,
        "timelines": [
            {"timeline": "1", "parent": None, "switchpoint_lsn": None},
        ],
    }
    with pytest.raises(ManifestError) as exc:
        parse_timelines(raw)
    assert "时间线 id 非法" in str(exc.value)


def test_timeline_requires_parent_switchpoint():
    raw = {
        "segment_size": SEG,
        "timelines": [
            {"timeline": "00000001", "parent": None},
            {"timeline": "00000002", "parent": "00000001"},
        ],
    }
    with pytest.raises(ManifestError, match="必须有一个根|switchpoint"):
        parse_timelines(raw)


def test_timeline_cycle_rejected():
    raw = {
        "segment_size": SEG,
        "timelines": [
            {"timeline": "00000001", "parent": "00000002",
             "switchpoint_lsn": "0/100000"},
            {"timeline": "00000002", "parent": "00000001",
             "switchpoint_lsn": "0/100000"},
        ],
    }
    with pytest.raises(ManifestError):
        parse_timelines(raw)


def test_timeline_switchpoint_must_increase():
    raw = {
        "segment_size": SEG,
        "timelines": [
            {"timeline": "00000001", "parent": None},
            {"timeline": "00000002", "parent": "00000001",
             "switchpoint_lsn": "0/400000"},
            {"timeline": "00000003", "parent": "00000002",
             "switchpoint_lsn": "0/300000"},
        ],
    }
    with pytest.raises(ManifestError, match="必须晚于"):
        parse_timelines(raw)


def test_segment_size_must_be_power_of_two():
    raw = {"segment_size": 3 * 1024 * 1024, "timelines": [
        {"timeline": "00000001", "parent": None}]}
    with pytest.raises(ManifestError, match="2 的幂"):
        parse_timelines(raw)
    raw = {"segment_size": 1000000, "timelines": [
        {"timeline": "00000001", "parent": None}]}
    with pytest.raises(ManifestError, match="必须在"):
        parse_timelines(raw)


def test_unsafe_paths_rejected():
    with pytest.raises(ManifestError):
        core.safe_rel_path("../etc/passwd", "x")
    with pytest.raises(ManifestError):
        core.safe_rel_path("/abs", "x")
    with pytest.raises(ManifestError):
        core.safe_rel_path("a\\b", "x")
    assert core.safe_rel_path("wal/00000001", "x") == "wal/00000001"
