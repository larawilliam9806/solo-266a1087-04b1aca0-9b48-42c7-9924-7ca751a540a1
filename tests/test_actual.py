"""实到 CSV 解析与停站对齐的单元测试（纯标准库路径）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.actual import (
    ON_TIME_SECS,
    TOLERANCE_SECS,
    align_records,
    parse_actual_csv,
)


def t(h, m=0, s=0):
    return h * 3600 + m * 60 + s


# 环线：S1 在序号 1、5 两次经过
PLANNED = [
    {"stop_sequence": 1, "stop_id": "S1", "arrival_secs": t(23)},
    {"stop_sequence": 2, "stop_id": "S2", "arrival_secs": t(23, 25)},
    {"stop_sequence": 3, "stop_id": "S3", "arrival_secs": t(24, 20)},
    {"stop_sequence": 4, "stop_id": "S4", "arrival_secs": t(24, 50)},
    {"stop_sequence": 5, "stop_id": "S1", "arrival_secs": t(25)},
]


# ---------- CSV 解析 ----------

def test_parse_valid_sorted_by_actual_time():
    recs, errors = parse_actual_csv(
        b"stop_id,actual_time\nS3,24:21:00\nS1,23:31:00\n")
    assert errors == []
    assert [r["stop_id"] for r in recs] == ["S1", "S3"]
    assert recs[0]["actual_secs"] == t(23, 31)
    assert recs[0]["actual_time"] == "23:31:00"
    assert recs[0]["row"] == 3  # CSV 文件行号


def test_parse_after_midnight_time():
    recs, errors = parse_actual_csv(
        b"stop_id,actual_time\nS1,25:10:00\nS2,26:00:00\n")
    assert errors == []
    assert [r["actual_secs"] for r in recs] == [t(25, 10), t(26)]


def test_parse_alias_columns_and_extra_columns():
    recs, errors = parse_actual_csv(
        "stop,actual,note\nS1,23:31:00,备注\n".encode())
    assert errors == []
    assert recs[0]["stop_id"] == "S1"


def test_parse_missing_column():
    recs, errors = parse_actual_csv(b"stop_id,note\nS1,x\n")
    assert recs is None
    assert errors and "缺少必备列" in errors[0]["message"]


def test_parse_duplicate_actual_time_rejected():
    recs, errors = parse_actual_csv(
        b"stop_id,actual_time\nS1,23:31:00\nS2,23:31:00\n")
    assert recs is None
    assert any("重复" in e["message"] and e["row"] == 3 for e in errors)


def test_parse_bad_time_and_empty_fields():
    for bad in [
        b"stop_id,actual_time\nS1,8:5:0\n",
        b"stop_id,actual_time\nS1,\n",
        b"stop_id,actual_time\n,23:31:00\n",
        b"stop_id,actual_time\n",
    ]:
        recs, errors = parse_actual_csv(bad)
        assert recs is None and errors


def test_parse_extra_values_row():
    recs, errors = parse_actual_csv(
        b"stop_id,actual_time\nS1,23:31:00,extra,extra2\n")
    assert recs is None
    assert any("列数多于表头" in e["message"] for e in errors)


# ---------- 对齐 ----------

def test_align_skip_unreported_stops():
    # 跳过 S2，其余按序对齐
    recs, _ = parse_actual_csv(
        b"stop_id,actual_time\nS1,23:30:00\nS3,24:21:00\nS4,24:48:00\n")
    out = align_records(PLANNED, recs)
    assert out["feasible"]
    assert [r["stop_sequence"] for r in out["results"]] == [1, 3, 4]
    diffs = [r["diff_secs"] for r in out["results"]]
    assert diffs == [TOLERANCE_SECS, 60, -120]
    assert all(r["status"] == "located" for r in out["results"])


def test_align_second_loop_stop_resolved_by_order():
    # 环线第二次经过 S1：前面的记录把序号推到 4 之后，只能落序号 5
    recs, _ = parse_actual_csv(
        b"stop_id,actual_time\nS4,24:55:00\nS1,25:02:00\n")
    out = align_records(PLANNED, recs)
    assert out["feasible"]
    assert [r["stop_sequence"] for r in out["results"]] == [4, 5]


def test_align_ambiguous_single_record_pending():
    # 单条 S1 记录同时落在两个同站序号的 30 分钟窗口内 -> 待核
    planned = [
        {"stop_sequence": 1, "stop_id": "S1", "arrival_secs": t(1)},
        {"stop_sequence": 2, "stop_id": "S2", "arrival_secs": t(1, 5)},
        {"stop_sequence": 3, "stop_id": "S1", "arrival_secs": t(1, 30)},
    ]
    recs, _ = parse_actual_csv(b"stop_id,actual_time\nS1,01:15:00\n")
    out = align_records(planned, recs)
    assert out["feasible"]
    r = out["results"][0]
    assert r["status"] == "pending"
    assert r["candidate_sequences"] == [1, 3]
    assert r["stop_sequence"] is None and r["diff_secs"] is None


def test_align_ambiguity_resolved_by_neighbor_constraint():
    # 同上歧义，但增加 S2 记录；严格递增约束把 S1 唯一确定
    planned = [
        {"stop_sequence": 1, "stop_id": "S1", "arrival_secs": t(1)},
        {"stop_sequence": 2, "stop_id": "S2", "arrival_secs": t(1, 5)},
        {"stop_sequence": 3, "stop_id": "S1", "arrival_secs": t(1, 30)},
    ]
    recs, _ = parse_actual_csv(
        b"stop_id,actual_time\nS2,01:06:00\nS1,01:15:00\n")
    out = align_records(planned, recs)
    assert out["feasible"]
    assert [(r["stop_id"], r["stop_sequence"]) for r in out["results"]] == [
        ("S2", 2), ("S1", 3)]


def test_align_unknown_stop_whole_trip_pending():
    recs, _ = parse_actual_csv(b"stop_id,actual_time\nSX,23:31:00\n")
    out = align_records(PLANNED, recs)
    assert out["feasible"] is False
    assert all(r["status"] == "pending" for r in out["results"])
    assert out["results"][0]["candidate_sequences"] == []


def test_align_outside_30min_window_infeasible():
    # 实到与最近的同站计划到站相差 60 分钟，超出窗口
    recs, _ = parse_actual_csv(b"stop_id,actual_time\nS2,22:25:00\n")
    out = align_records(PLANNED, recs)
    assert out["feasible"] is False
    assert all(r["status"] == "pending" for r in out["results"])


def test_align_boundary_30_minutes_allowed():
    # 恰好 30 分钟仍在窗口内
    recs, _ = parse_actual_csv(b"stop_id,actual_time\nS1,23:30:00\n")  # 计划 23:00
    out = align_records(PLANNED, recs)
    assert out["feasible"]
    assert out["results"][0]["status"] == "located"
    assert out["results"][0]["diff_secs"] == TOLERANCE_SECS


def test_align_sequence_must_strictly_increase():
    # 两条 S1 实到都只能匹配序号 1（第二条太晚，距序号 5 超窗口）-> 不可行
    recs, _ = parse_actual_csv(
        b"stop_id,actual_time\nS1,23:01:00\nS1,23:05:00\n")
    out = align_records(PLANNED, recs)
    assert out["feasible"] is False
    assert all(r["status"] == "pending" for r in out["results"])


def test_on_time_threshold():
    assert ON_TIME_SECS == 300
    recs, _ = parse_actual_csv(
        b"stop_id,actual_time\nS1,23:05:00\nS2,23:31:00\n")  # +5 分 / +6 分
    out = align_records(PLANNED, recs)
    flags = {r["stop_id"]: r["on_time"] for r in out["results"]}
    assert flags["S1"] is True
    assert flags["S2"] is False


def test_align_empty_inputs():
    out = align_records(PLANNED, [])
    assert out["feasible"] is False and out["results"] == []
