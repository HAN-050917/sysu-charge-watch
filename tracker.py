"""充电占用快照追踪器。

平台只给汇总数，所以我们自己累积时间线来推算「已充多久 / 还剩多久空出」：

  每 60 秒采集一次每台电柜的 chargeable（空闲数）
    Δ = 本次 - 上次
    Δ < 0  新增 |Δ| 个占用 → 入队 active（started_at = 本次采集时间）
    Δ > 0  有 Δ 个空出   → 从队首 FIFO 出队，落一条完整 session（minutes = now - started_at）

完整 session 的时长分布（按电柜 / 按点位两级）给出中位数与四分位，
仍在队列中的按 elapsed = now - started_at、remaining = median - elapsed 推算。

这是**推算**，不是平台官方数据。冷启动时没有历史，必须如实报告 confidence=none。
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Any

import app_paths

DB_PATH = app_paths.DB_PATH

# 无历史数据时的先验（分钟）。小程序可选时长 0.5~8 小时（"充满"按 8 小时计），
# 实测校园电动自行车多为 2~4 小时，取 180 分钟（3 小时）作先验中心。
PRIOR_MINUTES = 180.0

# 先验强度：相当于"先验里已有 20 条样本"。样本少时把中位数往先验收缩，
# 避免冷启动第一两条噪声样本（例如插上就拔）把整个模型带偏。
PRIOR_STRENGTH = 20.0

# 单条 session 的合理区间；超出 8 小时视为未拔插/漏采，剔除。
# 下限 10 分钟：低于这个的多数是插上就拔、误插或采集抖动，不是有效充电时长。
MIN_VALID_MINUTES = 10.0
MAX_VALID_MINUTES = 480.0

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cabinet_state (
                cabinet_no   TEXT PRIMARY KEY,
                chargeable   INTEGER NOT NULL,
                capacity     INTEGER NOT NULL,
                fault        INTEGER NOT NULL DEFAULT 0,
                exchangeable INTEGER NOT NULL DEFAULT 0,
                online       INTEGER NOT NULL,
                observed_at  REAL NOT NULL,
                location     TEXT,
                cabinet_type INTEGER,
                lng          REAL,
                lat          REAL,
                changed_at   REAL
            );
            CREATE TABLE IF NOT EXISTS charge_session (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cabinet_no  TEXT NOT NULL,
                started_at  REAL NOT NULL,
                ended_at    REAL NOT NULL,
                minutes     REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_session_cab ON charge_session(cabinet_no);
            CREATE TABLE IF NOT EXISTS cabinet_sample (
                cabinet_no  TEXT NOT NULL,
                observed_at REAL NOT NULL,
                chargeable  INTEGER NOT NULL,
                capacity    INTEGER NOT NULL,
                online      INTEGER NOT NULL,
                PRIMARY KEY (cabinet_no, observed_at)
            );
            CREATE INDEX IF NOT EXISTS idx_sample_time ON cabinet_sample(observed_at);
            CREATE TABLE IF NOT EXISTS poll_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at   REAL NOT NULL,
                cabinet_count INTEGER NOT NULL,
                ok            INTEGER NOT NULL,
                note          TEXT
            );
            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # active_occupancy 单独建，便于老库补 basis 列
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_occupancy (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cabinet_no  TEXT NOT NULL,
                started_at  REAL NOT NULL,
                basis       TEXT NOT NULL DEFAULT 'observed'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_active_cab ON active_occupancy(cabinet_no)"
        )
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(active_occupancy)")
        }
        if "basis" not in columns:
            conn.execute(
                "ALTER TABLE active_occupancy "
                "ADD COLUMN basis TEXT NOT NULL DEFAULT 'observed'"
            )


def get_kv(key: str, default: Any = None) -> Any:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return default


def set_kv(key: str, value: Any) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


def record_poll(rows: list[dict], observed_at: float | None = None) -> dict:
    """写入一次采集结果并更新占用队列。

    rows 需含 cabinet_no / chargeable / capacity / online，可选 fault / exchangeable /
    location / cabinet_type / lng / lat。
    """
    now = time.time() if observed_at is None else observed_at
    stats = {"new_occupancy": 0, "released": 0, "cabinets": len(rows)}
    with _lock, _connect() as conn:
        for row in rows:
            cabinet_no = row["cabinet_no"]
            chargeable = int(row["chargeable"])
            capacity = int(row["capacity"])
            online = 1 if row.get("online") else 0

            previous = conn.execute(
                "SELECT chargeable, online FROM cabinet_state WHERE cabinet_no = ?",
                (cabinet_no,),
            ).fetchone()

            if previous is not None and previous["online"] == 1 and online == 1:
                delta = chargeable - int(previous["chargeable"])
                if delta < 0:
                    conn.executemany(
                        "INSERT INTO active_occupancy(cabinet_no, started_at, basis) "
                        "VALUES(?, ?, 'observed')",
                        [(cabinet_no, now)] * (-delta),
                    )
                    stats["new_occupancy"] += -delta
                elif delta > 0:
                    pending = conn.execute(
                        "SELECT id, started_at, basis FROM active_occupancy "
                        "WHERE cabinet_no = ? ORDER BY started_at, id LIMIT ?",
                        (cabinet_no, delta),
                    ).fetchall()
                    for item in pending:
                        minutes = (now - float(item["started_at"])) / 60.0
                        conn.execute(
                            "DELETE FROM active_occupancy WHERE id = ?", (item["id"],)
                        )
                        stats["released"] += 1
                        # 只有"看见开始"的占用才能作为真实时长样本；
                        # backlog（观察前就已占用）只知道下界，不能污染中位数。
                        if item["basis"] != "observed":
                            stats["released_unmeasured"] = (
                                stats.get("released_unmeasured", 0) + 1
                            )
                            continue
                        if MIN_VALID_MINUTES <= minutes <= MAX_VALID_MINUTES:
                            conn.execute(
                                "INSERT INTO charge_session"
                                "(cabinet_no, started_at, ended_at, minutes) VALUES(?, ?, ?, ?)",
                                (cabinet_no, item["started_at"], now, minutes),
                            )

            # 补齐队列：观察开始前就已占用的桩位知道"现在占用"但不知道起点，
            # 用 backlog 标记入队，其 elapsed 只能作为下界，不参与时长分布。
            if online == 1:
                occupied_now = max(
                    0,
                    capacity
                    - chargeable
                    - min(int(row.get("fault") or 0), capacity),
                )
                known = conn.execute(
                    "SELECT COUNT(*) AS n FROM active_occupancy WHERE cabinet_no = ?",
                    (cabinet_no,),
                ).fetchone()["n"]
                missing = occupied_now - int(known)
                if missing > 0:
                    conn.executemany(
                        "INSERT INTO active_occupancy(cabinet_no, started_at, basis) "
                        "VALUES(?, ?, 'backlog')",
                        [(cabinet_no, now)] * missing,
                    )
                    stats["seeded"] = stats.get("seeded", 0) + missing

            conn.execute(
                "INSERT INTO cabinet_state"
                "(cabinet_no, chargeable, capacity, fault, exchangeable, online, observed_at,"
                " location, cabinet_type, lng, lat, changed_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cabinet_no) DO UPDATE SET "
                "chargeable = excluded.chargeable, capacity = excluded.capacity, "
                "fault = excluded.fault, exchangeable = excluded.exchangeable, "
                "online = excluded.online, observed_at = excluded.observed_at, "
                "location = excluded.location, cabinet_type = excluded.cabinet_type, "
                "lng = excluded.lng, lat = excluded.lat, "
                "changed_at = CASE WHEN cabinet_state.chargeable <> excluded.chargeable "
                "             THEN excluded.observed_at ELSE cabinet_state.changed_at END",
                (
                    cabinet_no,
                    chargeable,
                    capacity,
                    int(row.get("fault") or 0),
                    int(row.get("exchangeable") or 0),
                    online,
                    now,
                    row.get("location"),
                    row.get("cabinet_type"),
                    row.get("lng"),
                    row.get("lat"),
                    now,
                ),
            )

        conn.execute(
            "INSERT INTO poll_log(observed_at, cabinet_count, ok, note) VALUES(?, ?, 1, NULL)",
            (now, len(rows)),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO cabinet_sample"
            "(cabinet_no, observed_at, chargeable, capacity, online) VALUES(?, ?, ?, ?, ?)",
            [
                (
                    r["cabinet_no"],
                    now,
                    int(r["chargeable"]),
                    int(r["capacity"]),
                    1 if r.get("online") else 0,
                )
                for r in rows
            ],
        )
        conn.execute("DELETE FROM poll_log WHERE observed_at < ?", (now - 30 * 86400,))
        conn.execute("DELETE FROM cabinet_sample WHERE observed_at < ?", (now - 30 * 86400,))
    set_kv("last_poll_at", now)
    return stats


def record_poll_failure(note: str) -> None:
    now = time.time()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO poll_log(observed_at, cabinet_count, ok, note) VALUES(?, 0, 0, ?)",
            (now, note[:400]),
        )
    set_kv("last_poll_error", {"at": now, "note": note[:400]})


def all_cabinet_states() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT cabinet_no, chargeable, capacity, fault, exchangeable, online, "
            "       observed_at, location, cabinet_type, lng, lat, changed_at "
            "FROM cabinet_state"
        ).fetchall()
    return [dict(r) for r in rows]


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return PRIOR_MINUTES
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def duration_stats(cabinet_nos: list[str] | None = None) -> dict:
    """返回时长分布统计数据。cabinet_nos 为 None 时统计全部。

    样本少时中位数会向 PRIOR_MINUTES 收缩（贝叶斯式加权平均），
    避免冷启动阶段一两条噪声样本就给出离谱的估计值。
    observed_median_minutes 是未收缩的原始中位数，便于核对。
    """
    with _connect() as conn:
        if cabinet_nos:
            placeholders = ",".join("?" * len(cabinet_nos))
            rows = conn.execute(
                f"SELECT minutes FROM charge_session WHERE cabinet_no IN ({placeholders})",
                cabinet_nos,
            ).fetchall()
        else:
            rows = conn.execute("SELECT minutes FROM charge_session").fetchall()
    minutes = [float(r["minutes"]) for r in rows]
    samples = len(minutes)
    if samples == 0:
        return {
            "median_minutes": PRIOR_MINUTES,
            "observed_median_minutes": None,
            "p25_minutes": None,
            "p75_minutes": None,
            "samples": 0,
            "confidence": "none",
            "basis": "prior",
        }
    raw_median = statistics.median(minutes)
    shrunk = (raw_median * samples + PRIOR_MINUTES * PRIOR_STRENGTH) / (
        samples + PRIOR_STRENGTH
    )
    if samples >= 30:
        confidence = "high"
    elif samples >= 8:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "median_minutes": round(shrunk, 1),
        "observed_median_minutes": round(raw_median, 1),
        "p25_minutes": round(_quantile(minutes, 0.25), 1),
        "p75_minutes": round(_quantile(minutes, 0.75), 1),
        "samples": samples,
        "confidence": confidence,
        "basis": "observed" if samples >= 8 else "prior_blended",
    }


def active_occupancies(cabinet_nos: list[str] | None = None) -> dict[str, list[dict]]:
    """{cabinet_no: [{started_at, basis}, ...]}，按 started_at 升序。

    basis == "observed"  亲眼看见由空闲转为占用 → 起点准确，elapsed 可信
    basis == "backlog"   观察开始前已在占用 → 起点未知，started_at 只是"首次看见"的时间，
                         elapsed 只能当**下界**（真实值 >= 该值）
    """
    with _connect() as conn:
        if cabinet_nos:
            placeholders = ",".join("?" * len(cabinet_nos))
            rows = conn.execute(
                f"SELECT cabinet_no, started_at, basis FROM active_occupancy "
                f"WHERE cabinet_no IN ({placeholders}) ORDER BY started_at, id",
                cabinet_nos,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT cabinet_no, started_at, basis FROM active_occupancy "
                "ORDER BY started_at, id"
            ).fetchall()
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row["cabinet_no"], []).append(
            {"started_at": float(row["started_at"]), "basis": row["basis"]}
        )
    return out


def prune_impossible() -> int:
    """清理明显不可能仍在进行的占用（超过 8 小时未释放，多半是漏采或未拔插）。"""
    cutoff = time.time() - MAX_VALID_MINUTES * 60
    with _connect() as conn:
        cur = conn.execute("DELETE FROM active_occupancy WHERE started_at < ?", (cutoff,))
        return cur.rowcount


def series(cabinet_no: str, hours: int = 24) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT observed_at, chargeable, capacity, online FROM cabinet_sample "
            "WHERE cabinet_no = ? AND observed_at >= ? ORDER BY observed_at",
            (cabinet_no, time.time() - hours * 3600),
        ).fetchall()
    return [
        {
            "at": float(r["observed_at"]),
            "chargeable": r["chargeable"],
            "capacity": r["capacity"],
            "online": r["online"],
        }
        for r in rows
    ]


def cabinets_for_sites() -> list[dict]:
    """返回每台电柜最新的 cabinet_no 与其上报 location（用于按点位归组历史样本）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT cabinet_no, location FROM cabinet_state"
        ).fetchall()
    return [
        {"cabinet_no": r["cabinet_no"], "location": r["location"]}
        for r in rows
    ]


def site_samples(cabinet_nos: list[str], hours: int = 24,
                 start: float | None = None, end: float | None = None) -> list[dict]:
    """取出若干电柜在给定时间窗内的原始采样（用于区域级折线）。

    给了 start/end（Unix 秒）就用绝对窗口——按自然日查询时由调用方算出当天
    [0:00, 24:00) 的边界，曲线才能固定在 0:00–23:59；
    否则退化为「最近 hours 小时」的滚动窗口。
    """
    if not cabinet_nos:
        return []
    if start is None:
        start = time.time() - hours * 3600
    if end is None:
        # 略放宽到未来 1 小时，避免时钟漂移漏掉最新一条
        end = time.time() + 3600
    placeholders = ",".join("?" * len(cabinet_nos))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT observed_at, chargeable, capacity, online, cabinet_no FROM cabinet_sample "
            f"WHERE cabinet_no IN ({placeholders}) AND observed_at >= ? AND observed_at < ? "
            "ORDER BY observed_at",
            [*cabinet_nos, start, end],
        ).fetchall()
    return [
        {
            "at": float(r["observed_at"]),
            "chargeable": r["chargeable"],
            "capacity": r["capacity"],
            "online": r["online"],
            "cabinet_no": r["cabinet_no"],
        }
        for r in rows
    ]


def session_minutes(cabinet_nos: list[str] | None = None) -> list[float]:
    """取出用于时长建模的历史会话样本（分钟）。"""
    with _connect() as conn:
        if cabinet_nos:
            placeholders = ",".join("?" * len(cabinet_nos))
            rows = conn.execute(
                f"SELECT minutes FROM charge_session WHERE cabinet_no IN ({placeholders})",
                cabinet_nos,
            ).fetchall()
        else:
            rows = conn.execute("SELECT minutes FROM charge_session").fetchall()
    return [float(r["minutes"]) for r in rows]


def estimate_remaining(minutes: list[float], elapsed_minutes: float) -> dict:
    """纯函数版条件剩余估计，便于一次取样本、多次求值（避免每桩位一条 SQL）。"""
    elapsed = max(0.0, float(elapsed_minutes))
    survivors = [m for m in minutes if m > elapsed]
    if len(survivors) >= 8:
        total_estimate = statistics.median(survivors)
        basis = "conditional_median"
    else:
        # 指数衰减：长尾分布下的经验近似，保证随 elapsed 单调下降且 > 0
        total_estimate = elapsed + PRIOR_MINUTES * math.exp(-elapsed / PRIOR_MINUTES)
        basis = "exponential_prior"
    remaining = max(0.0, total_estimate - elapsed)
    return {
        "elapsed_minutes": round(elapsed, 1),
        "total_estimate_minutes": round(total_estimate, 1),
        "remaining_minutes": round(remaining, 1),
        "basis": basis,
        "survivors": len(survivors),
        "samples": len(minutes),
    }


def remaining_estimates(
    cabinet_nos: list[str] | None = None,
    elapsed_minutes: float | None = None,
) -> dict:
    """条件剩余时间估计：E[总时长 - 已用 | 总时长 > 已用]。

    朴素的 `median - elapsed` 有两个问题：
      1. 已用超过中位数后会给 0，但实际上仍有相当一部分会话还在进行；
      2. 忽略了"已经充了这么久还没结束"这一信息本身（生存偏差）。

    这里用历史会话的经验生存函数做条件化：取所有 > elapsed 的历史样本的中位数
    作为剩余总时长估计，再减去 elapsed。样本不足时退回指数衰减近似。

    单个电柜往往样本极少，不足 8 条时自动回退到全部会话的池子。
    """
    if elapsed_minutes is None:
        elapsed_minutes = 0.0
    minutes = session_minutes(cabinet_nos)
    scope = "cabinet" if cabinet_nos else "global"
    if cabinet_nos and len(minutes) < 8:
        minutes = session_minutes(None)
        scope = "global_fallback"
    result = estimate_remaining(minutes, elapsed_minutes)
    result["scope"] = scope
    return result


def health() -> dict:
    with _connect() as conn:
        sessions = conn.execute("SELECT COUNT(*) AS n FROM charge_session").fetchone()["n"]
        active = conn.execute("SELECT COUNT(*) AS n FROM active_occupancy").fetchone()["n"]
        cabinets = conn.execute("SELECT COUNT(*) AS n FROM cabinet_state").fetchone()["n"]
        polls = conn.execute("SELECT COUNT(*) AS n FROM poll_log WHERE ok = 1").fetchone()["n"]
        samples = conn.execute("SELECT COUNT(*) AS n FROM cabinet_sample").fetchone()["n"]
    last = get_kv("last_poll_at")
    return {
        "db": str(DB_PATH),
        "cabinets": cabinets,
        "active_occupancies": active,
        "completed_sessions": sessions,
        "successful_polls": polls,
        "samples": samples,
        "last_poll_at": last,
        "poll_age_seconds": round(time.time() - float(last), 1) if last else None,
    }
