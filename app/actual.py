"""实到时刻 CSV 解析与停站对齐（纯标准库，不依赖 FastAPI）。

业务背景：公交环线一趟可能两次经过同一站，车载记录只有站号和实到时刻，
因此一条记录可能对应计划停站序列中的多个序号。本模块枚举所有「可行对齐」
的边界，只把在所有可行对齐中都落到同一停站序号的记录计入统计。

对齐规则：
1. 记录按实到先后排序，与按 stop_sequence 排序的计划停站对齐；
   允许跳过未报站的计划停站，但停站序号必须严格递增。
2. 记录站号必须与计划停站站号相同，且实到与计划到站相差不超过 30 分钟。
3. 前向贪心求出每条记录在可行对齐中能落到的「最早」计划序号 lo，
   后向贪心求出「最晚」计划序号 hi；lo == hi 才表示该记录在所有可行
   对齐中位置唯一（状态 located），否则标记为待核（pending）。
4. 不存在覆盖全部记录的可行对齐时，整趟所有记录均为待核。

CSV 格式（表头必备两列，允许额外列）：
    stop_id,actual_time
    S1,23:31:00
- actual_time 为 HH:MM:SS，小时允许 >= 24（跨午夜沿用 GTFS 写法）；
- 同趟实到时刻不得重复；至少一行数据。
"""
from __future__ import annotations

import csv
import io

from .gtfs import format_gtfs_time, parse_gtfs_time

TOLERANCE_SECS = 30 * 60      # 实到与计划到站最大允许时差
ON_TIME_SECS = 5 * 60         # 时差绝对值 <= 5 分钟算准点

STOP_ID_ALIASES = ("stop_id", "stop")
ACTUAL_TIME_ALIASES = ("actual_time", "actual_arrival", "actual")
ACTUAL_FILE = "actual"


class ActualParseError(Exception):
    """实到 CSV 结构级错误（缺少列 / 空文件等）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _err(row: int | None, field: str | None, message: str) -> dict:
    return {"file": ACTUAL_FILE, "row": row, "field": field, "message": message}


def parse_actual_csv(content: bytes) -> tuple[list[dict] | None, list[dict]]:
    """解析实到 CSV。

    返回 (records, errors)：
    - 成功时 records 为按实到先后排序的记录列表
      [{row, stop_id, actual_secs, actual_time}]，errors 为空；
    - 失败时 records 为 None，errors 为错误明细（文件 / 行号 / 字段 / 原因）。
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, [_err(None, None, "文件不是有效的 UTF-8 编码")]

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return None, [_err(None, None, "文件为空或缺少表头")]
    headers = [h.strip() if h else "" for h in reader.fieldnames]

    stop_col = next((c for c in STOP_ID_ALIASES if c in headers), None)
    time_col = next((c for c in ACTUAL_TIME_ALIASES if c in headers), None)
    missing = []
    if stop_col is None:
        missing.append("stop_id")
    if time_col is None:
        missing.append("actual_time")
    if missing:
        return None, [_err(1, None, f"缺少必备列: {', '.join(missing)}")]

    errors: list[dict] = []
    records: list[dict] = []
    seen_times: dict[int, int] = {}
    for lineno, raw in enumerate(reader, start=2):
        if None in raw:  # 列数多于表头
            errors.append(_err(lineno, None, "行列数多于表头列数"))
            continue
        row = {k.strip(): (v.strip() if v is not None else "")
               for k, v in raw.items() if k is not None}
        stop_id = row.get(stop_col, "")
        actual_text = row.get(time_col, "")
        if not stop_id:
            errors.append(_err(lineno, stop_col, "stop_id 不能为空"))
        actual_secs = parse_gtfs_time(actual_text)
        if actual_secs is None:
            errors.append(_err(
                lineno, time_col,
                f"actual_time '{actual_text}' 不是合法的 HH:MM:SS 时刻（小时可 >= 24）"))
            continue
        if actual_secs in seen_times:
            errors.append(_err(
                lineno, time_col,
                f"实到时刻 {actual_text} 重复（首次出现于第 {seen_times[actual_secs]} 行），"
                "同趟实到时刻不得重复"))
            continue
        if stop_id:
            seen_times[actual_secs] = lineno
            records.append({"row": lineno, "stop_id": stop_id,
                            "actual_secs": actual_secs,
                            "actual_time": format_gtfs_time(actual_secs)})

    if not records and not errors:
        errors.append(_err(None, None, "文件没有数据行"))
    if errors:
        return None, errors
    records.sort(key=lambda r: r["actual_secs"])
    return records, []


def align_records(planned: list[dict], records: list[dict]) -> dict:
    """把实到记录与计划停站对齐。

    planned: [{"stop_sequence", "stop_id", "arrival_secs"}]，按 stop_sequence 升序；
    records: parse_actual_csv 成功返回的记录（已按实到升序）。

    返回 {"feasible": bool, "results": [...]}，results 与 records 同序，每项：
    {csv_row, stop_id, actual_time, actual_secs, status,
     stop_sequence, diff_secs, diff_minutes, on_time, candidate_sequences}
    """
    n, m = len(records), len(planned)
    candidates: list[list[int]] = []
    for r in records:
        cands = [
            j for j, p in enumerate(planned)
            if p["stop_id"] == r["stop_id"]
            and abs(r["actual_secs"] - p["arrival_secs"]) <= TOLERANCE_SECS
        ]
        candidates.append(cands)

    feasible = n > 0 and m > 0 and all(candidates)
    lo: list[int] = []
    hi: list[int] = []
    if feasible:
        # 前向贪心：每条记录取满足序号严格递增的最小候选序号
        prev = -1
        for c in candidates:
            nxt = next((j for j in c if j > prev), None)
            if nxt is None:
                feasible = False
                break
            lo.append(nxt)
            prev = nxt
    if feasible:
        # 后向贪心：每条记录取满足序号严格递增的最大候选序号
        hi = [0] * n
        nxt = m
        for i in range(n - 1, -1, -1):
            pick = None
            for j in reversed(candidates[i]):
                if j < nxt:
                    pick = j
                    break
            if pick is None:
                feasible = False
                break
            hi[i] = pick
            nxt = pick

    results: list[dict] = []
    for i, r in enumerate(records):
        base = {
            "csv_row": r["row"],
            "stop_id": r["stop_id"],
            "actual_time": r["actual_time"],
            "actual_secs": r["actual_secs"],
        }
        if feasible and lo[i] == hi[i]:
            p = planned[lo[i]]
            diff = r["actual_secs"] - p["arrival_secs"]
            results.append({
                **base,
                "status": "located",
                "stop_sequence": p["stop_sequence"],
                "diff_secs": diff,
                "diff_minutes": round(diff / 60, 1),
                "on_time": abs(diff) <= ON_TIME_SECS,
                "candidate_sequences": None,
            })
        else:
            results.append({
                **base,
                "status": "pending",
                "stop_sequence": None,
                "diff_secs": None,
                "diff_minutes": None,
                "on_time": None,
                "candidate_sequences": [planned[j]["stop_sequence"] for j in candidates[i]],
            })
    return {"feasible": feasible, "results": results}
