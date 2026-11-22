"""实到准点核算 API 集成测试。"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from conftest import upload, valid_files

DATE = "2026-11-23"  # 周一，WKD 与 ALL 均有效


def post_actual(client, csv_bytes, *, trip="N1", route="R2", date=DATE,
                version_id=None):
    data = {"route_id": route, "service_date": date, "trip_id": trip}
    if version_id is not None:
        data["version_id"] = version_id
    return client.post(
        "/api/actual-arrivals",
        data=data,
        files={"file": ("actual.csv", csv_bytes, "text/csv")},
    )


def test_upload_locate_and_stats(client):
    upload(client, valid_files(), label="v1")
    # N1 计划：S1 23:30 / S2 23:55 / S3 24:20 / S4 24:50；跳过 S2
    r = post_actual(client,
                    b"stop_id,actual_time\nS1,23:31:00\nS3,24:21:00\nS4,24:48:00\n")
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["feasible"] is True
    assert d["located_count"] == 3 and d["pending_count"] == 0
    seqs = [(x["stop_id"], x["stop_sequence"], x["diff_minutes"]) for x in d["records"]]
    assert seqs == [("S1", 1, 1.0), ("S3", 3, 1.0), ("S4", 4, -2.0)]
    assert [x["on_time"] for x in d["records"]] == [True, True, True]

    # 明细可回读，与保存时同一份数据
    got = client.get(f"/api/actual-arrivals/{d['upload_id']}").json()
    assert got["records"] == d["records"]

    # 按站统计：每站 1 条、全部准点
    stats = client.get("/api/actual-arrivals/stats").json()
    assert stats["total_located"] == 3 and stats["total_on_time"] == 3
    assert stats["overall_on_time_rate"] == 100.0
    by_stop = {s["stop_id"]: s for s in stats["stops"]}
    assert by_stop["S1"]["located_count"] == 1
    assert by_stop["S1"]["on_time_rate"] == 100.0
    assert by_stop["S1"]["stop_name"]  # 站名来自同版本 stops


def test_pending_rows_excluded_from_stats(client):
    upload(client, valid_files(), label="v1")
    # S1 晚点 10 分钟（不准点）；S4 准点
    post_actual(client,
                b"stop_id,actual_time\nS1,23:40:00\nS4,24:52:00\n")
    stats = client.get("/api/actual-arrivals/stats").json()
    by_stop = {s["stop_id"]: s for s in stats["stops"]}
    assert stats["total_located"] == 2 and stats["total_on_time"] == 1
    assert by_stop["S1"]["on_time_count"] == 0
    assert by_stop["S1"]["on_time_rate"] == 0.0
    assert stats["overall_on_time_rate"] == 50.0


def test_infeasible_whole_trip_pending(client):
    upload(client, valid_files(), label="v1")
    r = post_actual(client, b"stop_id,actual_time\nSX,23:31:00\n")
    assert r.status_code == 201
    d = r.json()
    assert d["feasible"] is False
    assert d["located_count"] == 0 and d["pending_count"] == 1
    assert all(x["status"] == "pending" for x in d["records"])
    # 无计入统计的记录
    stats = client.get("/api/actual-arrivals/stats").json()
    assert stats["stops"] == [] and stats["total_located"] == 0
    assert stats["overall_on_time_rate"] is None


def test_bad_csv_keeps_previous_records(client):
    upload(client, valid_files(), label="v1")
    ok = post_actual(client, b"stop_id,actual_time\nS1,23:31:00\n")
    assert ok.status_code == 201
    old_id = ok.json()["upload_id"]

    # 重复实到时刻 -> 422，旧数据保留
    r = post_actual(client,
                    b"stop_id,actual_time\nS1,23:31:00\nS2,23:31:00\n")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any("重复" in e["message"] for e in detail["errors"])
    assert "未保存" in detail["message"]

    # 坏时刻 / 缺列同样 422
    assert post_actual(client, b"stop_id,actual_time\nS1,8:5:0\n").status_code == 422
    assert post_actual(client, b"stop_id,time\nS1,23:31:00\n").status_code == 422

    # 上次记录与核算结果仍在
    got = client.get(f"/api/actual-arrivals/{old_id}").json()
    assert len(got["records"]) == 1 and got["records"][0]["status"] == "located"
    stats = client.get("/api/actual-arrivals/stats").json()
    assert stats["total_located"] == 1


def test_reupload_replaces_whole_trip(client):
    upload(client, valid_files(), label="v1")
    first = post_actual(client, b"stop_id,actual_time\nS1,23:31:00\n").json()
    second = post_actual(
        client, b"stop_id,actual_time\nS1,23:32:00\nS2,23:56:00\n").json()
    # 同 key 整份替换：列表只有一条，旧 upload 已删除
    lst = client.get("/api/actual-arrivals").json()
    assert lst["count"] == 1 and lst["uploads"][0]["upload_id"] == second["upload_id"]
    assert client.get(f"/api/actual-arrivals/{first['upload_id']}").status_code == 404
    got = client.get(f"/api/actual-arrivals/{second['upload_id']}").json()
    assert len(got["records"]) == 2
    # 统计随替换更新
    stats = client.get("/api/actual-arrivals/stats").json()
    assert stats["total_located"] == 2


def test_concurrent_corrections_different_trips_both_effective(client):
    upload(client, valid_files(), label="v1")
    results = {}

    def worker(trip, payload):
        r = post_actual(client, payload, trip=trip,
                        route="R1" if trip in ("T1", "T2") else "R2")
        results[trip] = r.status_code

    threads = [
        threading.Thread(target=worker, args=(
            "T1", b"stop_id,actual_time\nS1,08:02:00\nS2,08:11:00\n")),
        threading.Thread(target=worker, args=(
            "T2", b"stop_id,actual_time\nS4,10:01:00\nS3,10:31:00\n")),
        threading.Thread(target=worker, args=(
            "N1", b"stop_id,actual_time\nS1,23:31:00\n")),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results == {"T1": 201, "T2": 201, "N1": 201}

    lst = client.get("/api/actual-arrivals").json()
    assert {u["trip_id"] for u in lst["uploads"]} == {"T1", "T2", "N1"}
    # 明细与统计读取同一份已保存数据：共 5 条已定位
    stats = client.get("/api/actual-arrivals/stats").json()
    assert stats["total_located"] == 5
    # 按线路过滤
    r1_stats = client.get("/api/actual-arrivals/stats?route_id=R1").json()
    assert r1_stats["total_located"] == 4
    r2_stats = client.get("/api/actual-arrivals/stats?route_id=R2").json()
    assert r2_stats["total_located"] == 1


def test_select_version_route_date_validation(client):
    v1 = upload(client, valid_files(), label="v1").json()["version_id"]
    # 线路不存在
    r = post_actual(client, b"stop_id,actual_time\nS1,23:31:00\n",
                    route="NOPE", trip="N1")
    assert r.status_code == 404
    # 班次不属于该线路
    r = post_actual(client, b"stop_id,actual_time\nS1,23:31:00\n",
                    route="R1", trip="N1")
    assert r.status_code == 404
    # 班次在该运营日不运营：T1 仅 WKD，2026-11-21 是周六
    r = post_actual(client, b"stop_id,actual_time\nS1,08:02:00\n",
                    route="R1", trip="T1", date="2026-11-21")
    assert r.status_code == 422
    # 非法日期
    r = post_actual(client, b"stop_id,actual_time\nS1,08:02:00\n",
                    route="R1", trip="T1", date="2026-13-40")
    assert r.status_code == 422
    # 不存在的版本
    r = post_actual(client, b"stop_id,actual_time\nS1,23:31:00\n",
                    version_id=v1 + 999)
    assert r.status_code == 404


def test_actual_bound_to_published_version(client):
    v1 = upload(client, valid_files(), label="v1").json()["version_id"]
    post_actual(client, b"stop_id,actual_time\nS1,23:31:00\n", version_id=v1)

    # 发布第二版（只保留 R1/T1）
    files2 = valid_files(
        routes="route_id,route_short_name,route_long_name,route_type\n"
               "R1,1路快线,中央车站-机场,3\n",
        trips="route_id,service_id,trip_id\nR1,WKD,T1\n",
        stop_times="trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                   "T1,08:00:00,08:00:00,S1,1\n"
                   "T1,09:00:00,09:00:00,S4,2\n")
    v2 = upload(client, files2, label="v2").json()["version_id"]

    # 旧版本的实到数据仍可按版本读取
    lst = client.get(f"/api/actual-arrivals?version_id={v1}").json()
    assert lst["count"] == 1 and lst["uploads"][0]["version_id"] == v1
    # 当前版本（v2）看不到 v1 的更正
    cur = client.get("/api/actual-arrivals").json()
    assert cur["version_id"] == v2 and cur["count"] == 0
    # 统计同样按版本隔离
    stats = client.get(f"/api/actual-arrivals/stats?version_id={v1}").json()
    assert stats["total_located"] == 1
    # 历史版本时刻表接口仍可用
    sch = client.get(f"/api/routes/R2/schedule?date={DATE}&version_id={v1}").json()
    assert sch["trip_count"] == 1


def test_save_failure_keeps_previous(client, monkeypatch):
    upload(client, valid_files(), label="v1")
    ok = post_actual(client, b"stop_id,actual_time\nS1,23:31:00\n")
    assert ok.status_code == 201
    old_id = ok.json()["upload_id"]

    # 注入保存阶段异常：接口返回 500，旧记录保留
    from app import db
    monkeypatch.setattr(db, "replace_actual_upload",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    r = post_actual(client, b"stop_id,actual_time\nS2,23:56:00\n")
    assert r.status_code == 500
    got = client.get(f"/api/actual-arrivals/{old_id}").json()
    assert len(got["records"]) == 1
    stats = client.get("/api/actual-arrivals/stats").json()
    assert stats["total_located"] == 1


def test_list_and_date_filter(client):
    upload(client, valid_files(), label="v1")
    post_actual(client, b"stop_id,actual_time\nS1,23:31:00\n")
    # 按其它运营日过滤为空
    lst = client.get("/api/actual-arrivals?service_date=2026-11-24").json()
    assert lst["count"] == 0
    lst = client.get(f"/api/actual-arrivals?service_date={DATE}").json()
    assert lst["count"] == 1
