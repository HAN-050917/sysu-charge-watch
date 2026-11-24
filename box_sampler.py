"""固件实时状态采样器（queryBoxInfo）。

2026-11-19 逆向突破：桩端固件保留了两项关键信息，平台汇总接口不返回，
但 `POST /v1/user/queryBoxInfo`（需 token + mobile + port）能直接读到：

  cs  固件充电状态
        3 = 正在充电（实测 220V / 0.8A，有电流）
        6 = 到点断电（到达用户设定的目标时长后自动断电，电池未满）
        5 = 充电停止（用户手动结束 / 异常）
        0 = 已断电（含陈旧订单）
  ct  已充电分钟数
        cs=3 时随时间递增；断电后**冻结**在断电那一刻

决定性证据（对照实验，见 docs/逆向分析报告.md）：
  cs=6 的 12 个端口，ct 冻结值 = 480×11 + 240×1 → 12/12 精确命中
    小程序 timeList 的目标档位（480min=8h「充满」、240min=4h）
  cs=5 的 9 个端口，ct = 87/1/141/272/476/11/33/218/165 → 0/9 命中
⇒ 桩确实按用户选择的目标时长计时并到点断电。
⇒ cs=6 冻结的 ct 就是用户选择的目标时长。
⇒ 剩余时间 = 目标时长 − 已充时长，是**精确值**，不再依赖历史统计估计。

成本：单端口约 0.5s（并发 12），全量 400 端口需 ~214s，太重。
策略：轮转采样，每轮只查最需要精确数据的端口（占用中、优先无缓存的），
      单轮上限 `MAX_PER_ROUND`，缓存 `CACHE_TTL` 秒，保证 /api/state 快返。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import tosun_client

# 单轮最多查多少个端口。
# 实测（2026-11-20）并发 12 时 0.691s/端口，120 秒一轮可完成约 174 个。
# 原值 60 太小：全校区 372 个待采端口要 6 轮（约 12 分钟）才能覆盖一遍，
# 而明细每 120 秒刷新一次、缓存 110 秒就过期 —— cs=6 的端口往往还没轮到
# 就已经被新一轮覆盖掉了。180 让 372 个端口 2 轮（约 4 分钟）走完一遍。
MAX_PER_ROUND = 180

# 并发度。实测 12 是甜点：再高平台开始限流，收益不再增长。
WORKERS = 12

# 缓存"新鲜"有效期：与逐桩位明细刷新间隔（120s）对齐，决定是否要重采。
CACHE_TTL = 110.0

# 缓存"可读"有效期：展示时允许用多久之前的数据。
# 固件 ct 是慢变量（cs=3 时每分钟 +1），5 分钟前的读数远比退回历史统计准；
# cs=6 的冻结值更是永远不变。所以展示侧容忍更久的陈旧数据。
READ_TTL = 900.0

# 结果缓存 {(cabinet_no, port): {"box": {...}, "at": ts}}
_CACHE: dict[tuple[str, int], dict[str, Any]] = {}
_LOCK = threading.Lock()
_ROTATION = {"cursor": 0}


def clear() -> None:
    with _LOCK:
        _CACHE.clear()
        _ROTATION["cursor"] = 0


def _fresh(entry: dict[str, Any] | None, now: float) -> bool:
    return bool(entry) and (now - float(entry.get("at") or 0)) < CACHE_TTL


def cached(cabinet_no: str, port: int, max_age: float | None = None) -> dict[str, Any] | None:
    """取缓存的固件状态；不存在返回 None。

    `max_age` 默认用 `READ_TTL`（展示侧容忍 15 分钟内的旧读数）——
    不要用 `CACHE_TTL`：固件 `ct` 是慢变量，5 分钟前的读数也比退回历史统计准，
    而 `cs=6` 的冻结值永不变。用 110 秒会把绝大多数已采到的端口判成"没数据"，
    直接导致 `ports_precise=0` / `ports_blocked=0`。
    """
    key = (str(cabinet_no), int(port))
    age = READ_TTL if max_age is None else float(max_age)
    with _LOCK:
        entry = _CACHE.get(key)
        if not entry or not entry.get("box"):
            return None
        return entry["box"] if (time.time() - float(entry.get("at") or 0)) < age else None


def get_many(
    pairs: list[tuple[str, int]],
    token: str,
    mobile: str,
    budget: int = MAX_PER_ROUND,
) -> dict[tuple[str, int], dict[str, Any]]:
    """批量取固件状态，只查缓存过期/缺失的，最多 budget 个。

    返回本次**新查到**的结果（含缓存命中的也一并返回，方便调用方直接用）。
    """
    if not token or not str(mobile or "").strip():
        return {}
    now = time.time()
    todo: list[tuple[str, int]] = []
    result: dict[tuple[str, int], dict[str, Any]] = {}
    with _LOCK:
        for cab, port in pairs:
            key = (str(cab), int(port))
            entry = _CACHE.get(key)
            if _fresh(entry, now):
                result[key] = entry["box"]
            else:
                todo.append(key)

    if not todo:
        return result

    # 轮转：从上次停下的位置继续，保证每轮覆盖不同端口，避免长期饿死后面的柜
    with _LOCK:
        start = _ROTATION["cursor"] % len(todo)
    todo = todo[start:] + todo[:start]
    batch = todo[:budget]

    def _one(key: tuple[str, int]) -> tuple[tuple[str, int], dict[str, Any]]:
        cab, port = key
        try:
            box = tosun_client.box_info(cab, port, token, mobile)
        except Exception:
            box = {}
        return key, box

    fetched_at = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for key, box in pool.map(_one, batch):
            if box:
                result[key] = box
                with _LOCK:
                    _CACHE[key] = {"box": box, "at": fetched_at}
            else:
                # 查不到也记一笔空，避免同一轮反复重试打爆平台
                with _LOCK:
                    _CACHE[key] = {"box": {}, "at": fetched_at}

    with _LOCK:
        if todo:
            _ROTATION["cursor"] = (_ROTATION["cursor"] + len(batch)) % max(1, len(todo))
    return result


def stats() -> dict[str, Any]:
    now = time.time()
    with _LOCK:
        total = len(_CACHE)
        with_box = sum(1 for e in _CACHE.values() if e.get("box"))
        fresh = sum(1 for e in _CACHE.values() if _fresh(e, now))
        readable = sum(
            1
            for e in _CACHE.values()
            if e.get("box") and (now - float(e.get("at") or 0)) < READ_TTL
        )
        charged = sum(
            1
            for e in _CACHE.values()
            if e.get("box")
            and (now - float(e.get("at") or 0)) < READ_TTL
            and e["box"].get("cs") == tosun_client.CS_CHARGING
        )
        reached = sum(
            1
            for e in _CACHE.values()
            if e.get("box")
            and (now - float(e.get("at") or 0)) < READ_TTL
            and e["box"].get("cs") == tosun_client.CS_REACHED_TARGET
        )
    return {
        "cached_ports": total,
        "with_data": with_box,
        "fresh_ports": fresh,
        "readable_ports": readable,
        "charging": charged,
        "reached_target": reached,
        "cache_ttl_seconds": CACHE_TTL,
        "read_ttl_seconds": READ_TTL,
        "max_per_round": MAX_PER_ROUND,
    }
