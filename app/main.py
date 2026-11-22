"""FastAPI HTTP 层：版本上传发布 + 时刻查询 + 实到准点核算。"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date as date_type
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .actual import align_records, parse_actual_csv
from .gtfs import WEEKDAY_COLUMNS, format_gtfs_time, validate_version

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="公交 GTFS 时刻表发布", lifespan=lifespan)


# ---------------- 发布 ----------------

@app.post("/api/versions", status_code=201)
async def publish(
    label: str = Form(..., min_length=1, max_length=200),
    stops: UploadFile = File(...),
    routes: UploadFile = File(...),
    trips: UploadFile = File(...),
    stop_times: UploadFile = File(...),
    calendar: UploadFile = File(...),
):
    """上传同一版本的 5 个 CSV。整版校验通过才发布，否则记录错误并保持当前正式版。"""
    uploads = {"stops": stops, "routes": routes, "trips": trips,
               "stop_times": stop_times, "calendar": calendar}
    contents: dict[str, bytes] = {}
    for name, up in uploads.items():
        contents[name] = await up.read()

    tables, errors, truncated = validate_version(contents)
    conn = db.connect()
    try:
        if tables is None:
            vid = db.record_failed_version(conn, label, errors)
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "版本校验失败，未发布；当前正式版保持不变",
                    "version_id": vid,
                    "error_count": len(errors),
                    "truncated": truncated,
                    "errors": errors,
                })
        vid = db.publish_version(conn, label, tables)
        counts = {name: len(rows) for name, rows in tables.items()}
    finally:
        conn.close()
    return {"message": "版本已发布", "version_id": vid, "label": label,
            "status": "published", "counts": counts}


# ---------------- 版本信息 ----------------

@app.get("/api/versions")
def list_versions():
    conn = db.connect()
    try:
        cur_id = db.current_version_id(conn)
        rows = conn.execute(
            "SELECT id, label, status, error_count, created_at, published_at "
            "FROM versions ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    return {"current_version_id": cur_id,
            "versions": [dict(r) for r in rows]}


@app.get("/api/versions/current")
def current_version():
    conn = db.connect()
    try:
        vid = db.current_version_id(conn)
        if vid is None:
            raise HTTPException(404, "尚未发布任何版本")
        row = conn.execute(
            "SELECT id, label, status, created_at, published_at "
            "FROM versions WHERE id = ?", (vid,)).fetchone()
    finally:
        conn.close()
    return dict(row)


@app.get("/api/versions/{version_id}")
def version_detail(version_id: int):
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT id, label, status, error_count, created_at, published_at "
            "FROM versions WHERE id = ?", (version_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"版本 {version_id} 不存在")
        errors = conn.execute(
            "SELECT file, row, field, message FROM version_errors "
            "WHERE version_id = ? ORDER BY id", (version_id,)).fetchall()
    finally:
        conn.close()
    result = dict(row)
    result["errors"] = [dict(e) for e in errors]
    return result


# ---------------- 查询（始终读取同一完整正式版） ----------------

def _require_current(conn) -> int:
    vid = db.current_version_id(conn)
    if vid is None:
        raise HTTPException(404, "尚未发布任何版本")
    return vid


def _resolve_version(conn, version_id: int | None) -> int:
    """未指定版本时使用当前正式版；指定时校验版本存在且已发布。"""
    if version_id is None:
        return _require_current(conn)
    row = conn.execute(
        "SELECT id, status FROM versions WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"版本 {version_id} 不存在")
    if row["status"] != "published":
        raise HTTPException(404, f"版本 {version_id} 不是已发布版本")
    return version_id


@app.get("/api/routes")
def list_routes(version_id: int | None = Query(None, description="版本 ID，默认当前正式版")):
    conn = db.connect()
    try:
        vid = _resolve_version(conn, version_id)
        rows = conn.execute(
            "SELECT route_id, route_short_name, route_long_name, route_type "
            "FROM routes WHERE version_id = ? ORDER BY route_id", (vid,)).fetchall()
    finally:
        conn.close()
    return {"version_id": vid, "routes": [dict(r) for r in rows]}


@app.get("/api/routes/{route_id}/schedule")
def route_schedule(
    route_id: str,
    date: str = Query(..., description="运营日，格式 YYYY-MM-DD"),
    version_id: int | None = Query(None, description="版本 ID，默认当前正式版"),
):
    """按线路 + 运营日查询有序停站时刻。

    时刻以 GTFS 形式返回（小时可 >= 24，表示跨午夜后仍属该运营日），
    并附 day_offset（相对运营日的天数偏移）与实际钟点。
    """
    try:
        svc_date = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, f"date '{date}' 不是合法的 YYYY-MM-DD 日期")
    weekday_col = WEEKDAY_COLUMNS[svc_date.weekday()]

    conn = db.connect()
    try:
        # 请求开始时固定版本，之后所有查询都固定在这个完整版本上
        vid = _resolve_version(conn, version_id)
        route = conn.execute(
            "SELECT route_id, route_short_name, route_long_name, route_type "
            "FROM routes WHERE version_id = ? AND route_id = ?",
            (vid, route_id)).fetchone()
        if route is None:
            raise HTTPException(404, f"线路 '{route_id}' 在当前版本 {vid} 中不存在")

        services = conn.execute(
            f"SELECT service_id FROM calendar WHERE version_id = ? "
            f"AND start_date <= ? AND end_date >= ? AND {weekday_col} = 1",
            (vid, date, date)).fetchall()
        service_ids = [r["service_id"] for r in services]

        trips_out = []
        if service_ids:
            placeholders = ",".join("?" for _ in service_ids)
            trips = conn.execute(
                f"SELECT trip_id, service_id, trip_headsign, direction_id "
                f"FROM trips WHERE version_id = ? AND route_id = ? "
                f"AND service_id IN ({placeholders})",
                (vid, route_id, *service_ids)).fetchall()
            for t in trips:
                stops = conn.execute(
                    "SELECT st.stop_sequence, st.stop_id, s.stop_name, "
                    "st.arrival_secs, st.departure_secs "
                    "FROM stop_times st JOIN stops s "
                    "  ON s.version_id = st.version_id AND s.stop_id = st.stop_id "
                    "WHERE st.version_id = ? AND st.trip_id = ? "
                    "ORDER BY st.stop_sequence",
                    (vid, t["trip_id"])).fetchall()
                trips_out.append({
                    "trip_id": t["trip_id"],
                    "service_id": t["service_id"],
                    "headsign": t["trip_headsign"],
                    "direction_id": t["direction_id"],
                    "stop_times": [_stop_time_dict(s) for s in stops],
                })
            trips_out.sort(
                key=lambda t: t["stop_times"][0]["departure_secs"]
                if t["stop_times"] else 0)
    finally:
        conn.close()

    return {
        "version_id": vid,
        "route": dict(route),
        "date": date,
        "weekday": weekday_col,
        "trip_count": len(trips_out),
        "trips": trips_out,
    }


def _stop_time_dict(row) -> dict:
    arr, dep = row["arrival_secs"], row["departure_secs"]
    return {
        "stop_sequence": row["stop_sequence"],
        "stop_id": row["stop_id"],
        "stop_name": row["stop_name"],
        "arrival": format_gtfs_time(arr),
        "departure": format_gtfs_time(dep),
        "arrival_secs": arr,
        "departure_secs": dep,
        "arrival_day_offset": arr // 86400,
        "departure_day_offset": dep // 86400,
        "arrival_clock": format_gtfs_time(arr % 86400),
        "departure_clock": format_gtfs_time(dep % 86400),
    }


# ---------------- 实到准点核算 ----------------

def _planned_trip(conn, vid: int, route_id: str, service_date: str,
                  trip_id: str, weekday_col: str) -> list[dict]:
    """取指定班次在运营日的计划停站；校验版本/线路/运营服务/班次存在性。"""
    route = conn.execute(
        "SELECT route_id FROM routes WHERE version_id = ? AND route_id = ?",
        (vid, route_id)).fetchone()
    if route is None:
        raise HTTPException(404, f"线路 '{route_id}' 在版本 {vid} 中不存在")
    trip = conn.execute(
        "SELECT t.trip_id, c.service_id FROM trips t "
        "JOIN calendar c ON c.version_id = t.version_id "
        "  AND c.service_id = t.service_id "
        "WHERE t.version_id = ? AND t.route_id = ? AND t.trip_id = ?",
        (vid, route_id, trip_id)).fetchone()
    if trip is None:
        raise HTTPException(404,
                            f"班次 '{trip_id}' 在版本 {vid} 的线路 '{route_id}' 中不存在")
    active = conn.execute(
        f"SELECT 1 FROM calendar WHERE version_id = ? AND service_id = ? "
        f"AND start_date <= ? AND end_date >= ? AND {weekday_col} = 1",
        (vid, trip["service_id"], service_date, service_date)).fetchone()
    if active is None:
        raise HTTPException(422,
                            f"班次 '{trip_id}' 的服务 '{trip['service_id']}' "
                            f"在运营日 {service_date} 不运营")
    rows = conn.execute(
        "SELECT stop_sequence, stop_id, arrival_secs FROM stop_times "
        "WHERE version_id = ? AND trip_id = ? ORDER BY stop_sequence",
        (vid, trip_id)).fetchall()
    return [dict(r) for r in rows]


def _record_dict(row) -> dict:
    cands = row["candidate_sequences"]
    return {
        "csv_row": row["csv_row"],
        "stop_id": row["stop_id"],
        "actual_time": format_gtfs_time(row["actual_secs"]),
        "actual_secs": row["actual_secs"],
        "status": row["status"],
        "stop_sequence": row["stop_sequence"],
        "diff_secs": row["diff_secs"],
        "diff_minutes": None if row["diff_secs"] is None
        else round(row["diff_secs"] / 60, 1),
        "on_time": None if row["on_time"] is None else bool(row["on_time"]),
        "candidate_sequences": json.loads(cands) if cands else None,
    }


def _upload_detail_payload(conn, upload) -> dict:
    recs = db.list_actual_records(conn, upload["id"])
    return {
        "upload_id": upload["id"],
        "version_id": upload["version_id"],
        "trip_id": upload["trip_id"],
        "service_date": upload["service_date"],
        "feasible": bool(upload["feasible"]),
        "located_count": upload["located_count"],
        "pending_count": upload["pending_count"],
        "uploaded_at": upload["uploaded_at"],
        "records": [_record_dict(r) for r in recs],
    }


@app.post("/api/actual-arrivals", status_code=201)
async def upload_actual(
    version_id: int | None = Form(None, description="运行图版本，默认当前正式版"),
    route_id: str = Form(..., min_length=1, max_length=100),
    service_date: str = Form(..., description="运营日 YYYY-MM-DD"),
    trip_id: str = Form(..., min_length=1, max_length=100),
    file: UploadFile = File(..., description="实到 CSV：stop_id,actual_time"),
):
    """上传或整份更正某班次的实到时刻 CSV，保存对齐与准点核算结果。

    格式错误时返回 422 并保留上次记录；保存成功才替换旧数据。
    """
    try:
        date_type.fromisoformat(service_date)
    except ValueError:
        raise HTTPException(422,
                            f"service_date '{service_date}' 不是合法的 YYYY-MM-DD 日期")
    weekday_col = WEEKDAY_COLUMNS[date_type.fromisoformat(service_date).weekday()]
    content = await file.read()

    records, errors = parse_actual_csv(content)
    if records is None:
        raise HTTPException(422, detail={
            "message": "实到 CSV 格式错误，未保存；上次记录与核算结果保持不变",
            "error_count": len(errors), "errors": errors})

    conn = db.connect()
    try:
        vid = _resolve_version(conn, version_id)
        planned = _planned_trip(conn, vid, route_id, service_date, trip_id, weekday_col)
        alignment = align_records(planned, records)
        try:
            upload_id = db.replace_actual_upload(
                conn, vid, trip_id, service_date,
                alignment["feasible"], alignment["results"])
        except Exception as exc:  # 保存失败：事务回滚，旧数据不动
            conn.rollback()
            raise HTTPException(500, f"实到数据保存失败，已保留上次记录：{exc}")
        payload = _upload_detail_payload(
            conn, db.get_actual_upload(conn, upload_id))
    finally:
        conn.close()
    payload["message"] = "实到记录已保存"
    return payload


@app.get("/api/actual-arrivals")
def list_actual(
    version_id: int | None = Query(None),
    route_id: str | None = Query(None),
    service_date: str | None = Query(None),
):
    """已更正班次列表（可按版本 / 线路 / 运营日过滤）。"""
    conn = db.connect()
    try:
        vid = _resolve_version(conn, version_id)
        rows = db.list_actual_uploads(conn, vid, route_id, service_date)
        out = [{
            "upload_id": r["id"], "version_id": r["version_id"],
            "trip_id": r["trip_id"], "service_date": r["service_date"],
            "feasible": bool(r["feasible"]),
            "located_count": r["located_count"],
            "pending_count": r["pending_count"],
            "uploaded_at": r["uploaded_at"],
        } for r in rows]
    finally:
        conn.close()
    return {"version_id": vid, "count": len(out), "uploads": out}


@app.get("/api/actual-arrivals/stats")
def actual_stats(
    version_id: int | None = Query(None),
    route_id: str | None = Query(None),
    service_date: str | None = Query(None),
):
    """按站统计已定位记录条数与准点率；明细和统计读取同一份已保存数据。"""
    conn = db.connect()
    try:
        vid = _resolve_version(conn, version_id)
        rows = db.stop_punctuality_stats(conn, vid, route_id, service_date)
        stops = [dict(r) for r in rows]
        total = sum(r["located_count"] for r in rows)
        on_time = sum(r["on_time_count"] for r in rows)
    finally:
        conn.close()
    return {
        "version_id": vid,
        "route_id": route_id,
        "service_date": service_date,
        "on_time_threshold_secs": 5 * 60,
        "total_located": total,
        "total_on_time": on_time,
        "overall_on_time_rate": round(100.0 * on_time / total, 2) if total else None,
        "stops": stops,
    }


@app.get("/api/actual-arrivals/{upload_id}")
def actual_detail(upload_id: int):
    """单趟更正的明细：每条记录的停站序号、时差、准点标志与待核候选序号。"""
    conn = db.connect()
    try:
        upload = db.get_actual_upload(conn, upload_id)
        if upload is None:
            raise HTTPException(404, f"实到上传记录 {upload_id} 不存在")
        payload = _upload_detail_payload(conn, upload)
    finally:
        conn.close()
    return payload


# ---------------- 网页 ----------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
