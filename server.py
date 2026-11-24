"""SYSU Charge Watch — 本地只读看板服务。

用法:
    python server.py                 # 默认 127.0.0.1:8787
    python server.py --port 9000
    python server.py --once          # 只采集一次后退出（自检/定时任务）
    python server.py --no-poll       # 不启动后台采集（读已有 DB 演示）

只做三件事：采集公开的附近电柜汇总数 → 存快照时间线 → 提供只读 HTTP API。
不登录、不支付、不下单、不启停充电。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import tosun_client
import tracker
import box_sampler
import target_model
from api_routes import Api

import app_paths

ROOT = app_paths.APP_ROOT
WEB = app_paths.WEB_DIR
CONFIG_PATH = app_paths.CONFIG_PATH
PROFILE_CACHE = app_paths.PROFILE_CACHE
SITES_CFG = json.loads(app_paths.SITES_PATH.read_text(encoding="utf-8"))

CST = timezone(timedelta(hours=8))
POLL_INTERVAL = 60
STALE_AFTER = 150
MIN_POLL_GAP = 20

STATE_LOCK = threading.Lock()
LAST_SNAPSHOT: dict[str, object] = {}
PROFILES: dict[str, dict] = {}
POLL_META = {"last_attempt": None, "last_ok": None, "last_error": None, "running": False}

# 登录 token 解锁的逐桩位真实数据（仅在用户手动配置 token 后启用）
AUTH_LOCK = threading.Lock()
AUTH_STATE: dict[str, object] = {
    "valid": None,
    "reason": None,
    "checked_at": None,
    "mobile": "",           # 抓包得到的真实手机号，逐桩位接口必传
    "mobile_masked": "",
    "details": {},          # {cabinetNo: [cabox, ...]}
    "errors": {},           # {cabinetNo: reason}
    "details_at": None,
    "last_error": None,
    "refreshing": False,    # 后台正在拉取逐桩位真实数据
}
DETAIL_REFRESH_SECONDS = 55  # 略小于 poll_interval(60)，保证每轮采集都跟一次明细，不出现跨轮延迟
AUTH_BG_LOCK = threading.Lock()   # 防止并发起多个后台刷新线程


# ---------------------------------------------------------------- config

def load_config() -> dict:
    defaults = {
        "poll_interval": POLL_INTERVAL,
        "token": "",
        "mobile": "",   # 逐桩位接口必传；抓包时从请求体一并取到
        "scan_distance_km": SITES_CFG["campus"].get("scan_distance_km", 3),
    }
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    if isinstance(raw, dict):
        defaults.update({k: v for k, v in raw.items() if k in defaults})
    return defaults


def save_config(patch: dict) -> dict:
    cfg = load_config()
    for key in ("poll_interval", "token", "mobile", "scan_distance_km"):
        if key in patch:
            cfg[key] = patch[key]
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


# ---------------------------------------------------------------- auth (逐桩位真实数据)

def _mask_mobile(value) -> str:
    s = str(value or "")
    return s[:3] + "****" + s[-4:] if len(s) >= 7 else ("*" * len(s))


def check_auth(force: bool = False) -> dict:
    """用当前 token 验证一次会话，并刷新 AUTH_STATE 的 effective 状态。

    不抛异常；所有失败都记录为 valid=False + reason。
    """
    cfg = load_config()
    token = str(cfg.get("token") or "").strip()
    now = time.time()
    if not token:
        with AUTH_LOCK:
            AUTH_STATE.update({"valid": False, "reason": "未配置 token", "checked_at": now,
                               "mobile_masked": "", "last_error": None})
        return auth_status()
    # 非强制时 30 秒内复用上次结果，避免前端频繁点击打爆平台
    with AUTH_LOCK:
        checked = AUTH_STATE.get("checked_at")
        if not force and checked and now - float(checked) < 30 and AUTH_STATE.get("valid") is not None:
            return auth_status()
    result = tosun_client.check_token(token)
    # checkTokenValid 实测返回 data=None，拿不到 user/mobile；手机号由抓包一并取到，
    # 存于 config["mobile"]，逐桩位接口必传。
    cfg_mobile = str(cfg.get("mobile") or "").strip()
    with AUTH_LOCK:
        AUTH_STATE["valid"] = bool(result.get("valid"))
        AUTH_STATE["reason"] = result.get("reason")
        AUTH_STATE["checked_at"] = now
        user = result.get("user") or {}
        AUTH_STATE["user"] = user if result.get("valid") else {}
        AUTH_STATE["mobile"] = cfg_mobile if result.get("valid") else ""
        AUTH_STATE["mobile_masked"] = _mask_mobile(
            cfg_mobile or user.get("mobile") or user.get("phone") or user.get("userName")
        ) if result.get("valid") else ""
        AUTH_STATE["last_error"] = None if result.get("valid") else result.get("reason")
        if not result.get("valid"):
            # token 失效：清空逐桩位缓存，避免展示过期真实数据
            AUTH_STATE["details"] = {}
            AUTH_STATE["errors"] = {}
            AUTH_STATE["details_at"] = None
    return auth_status()


def refresh_details(force: bool = False) -> dict:
    """token 有效时，拉取校园各电柜的逐桩位真实状态。

    限频 DETAIL_REFRESH_SECONDS；token 失效或缺失时直接跳过。
    """
    cfg = load_config()
    token = str(cfg.get("token") or "").strip()
    if not token:
        return {"ok": False, "reason": "未配置 token"}
    with AUTH_LOCK:
        if not AUTH_STATE.get("valid"):
            return {"ok": False, "reason": str(AUTH_STATE.get("reason") or "token 未验证")}
        now = time.time()
        details_at = AUTH_STATE.get("details_at")
        if not force and details_at and now - float(details_at) < DETAIL_REFRESH_SECONDS:
            return {"ok": True, "cached": True}
        nos = list((AUTH_STATE.get("details") or {}).keys()) or [
            i["cabinetNo"] for i in (LAST_SNAPSHOT.get("items") or []) if i.get("cabinetNo")
        ]
    if not nos:
        return {"ok": False, "reason": "无可查询的电柜"}
    mobile = str(AUTH_STATE.get("mobile") or "").strip() or str(cfg.get("mobile") or "").strip()
    if not mobile:
        return {"ok": False, "reason": "缺少手机号（逐桩位接口必传）"}
    details, errors = tosun_client.cabinet_details(nos, token, mobile=mobile)
    with AUTH_LOCK:
        AUTH_STATE["details"] = details
        AUTH_STATE["errors"] = errors
        AUTH_STATE["details_at"] = time.time()
        if errors and not details:
            # 全部柜失败：多半是 token 中途失效
            first_reason = next(iter(errors.values()), "")
            AUTH_STATE["valid"] = False
            AUTH_STATE["reason"] = first_reason
            AUTH_STATE["last_error"] = first_reason
            AUTH_STATE["details"] = {}
    # 逐桩位明细到位后，后台补采固件实时状态（queryBoxInfo 很重，绝不阻塞这里）
    if details:
        _schedule_box_sampling(details, token, mobile)
    return {"ok": bool(details), "cabinets": len(details), "errors": len(errors)}


BOX_SAMPLE_LOCK = threading.Lock()


def _schedule_box_sampling(details: dict[str, list[dict]], token: str, mobile: str) -> None:
    """后台线程补采固件实时状态（cs/ct/电压/电流）。

    采样范围不能只挑「平台判定占用」的端口 —— 实测发现 cs=6（到点断电、
    电池未取走）时平台 `chargerStatus=6`、判定为**空闲**，但物理桩位仍被
    电池占着。若只采占用端口，就永远发现不了这类「假空闲」。
    因此：占用端口必采 + 平台判空闲但 chargerStatus 异常（非 0/1）的也采。
    """
    pairs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    # 异常端口（cs=6 到点断电 / 5 手动停止 / 8 故障）优先入队：
    # 它们数量少（实测 19+23+8 ≈ 50 个），但决定了 `blocked` 与精确剩余能否显示。
    # 放在最前面保证第一轮采样就覆盖到，用户不必等 2~4 分钟才看到"已断电未取走"。
    abnormal: list[tuple[str, int]] = []
    for cab, caboxs in details.items():
        for cabox in caboxs or []:
            if not isinstance(cabox, dict):
                continue
            try:
                port = int(cabox.get("port"))
            except (TypeError, ValueError):
                continue
            occupied = tosun_client.is_cabox_occupied(cabox)
            try:
                charger_status = int(cabox.get("chargerStatus"))
            except (TypeError, ValueError):
                charger_status = -1
            # 占用中 → 必采；平台判空闲但 chargerStatus=6/8 等异常 → 也要采
            if not occupied and charger_status in (0, 1, 7, -1):
                continue
            key = (cab, port)
            if key in seen:
                continue
            seen.add(key)
            if charger_status in (5, 6, 8, 10):
                abnormal.append(key)
            else:
                pairs.append(key)
    pairs = abnormal + pairs
    if not pairs:
        return

    def _worker() -> None:
        try:
            results = box_sampler.get_many(pairs, token, mobile)
            # 顺手学习目标时长分布：cs=6（到点断电）的冻结 ct 就是用户选的目标
            boxes = [r for r in (results or {}).values() if r]
            if boxes:
                target_model.observe(boxes)
        except Exception as exc:
            with AUTH_LOCK:
                AUTH_STATE["last_error"] = f"固件采样失败: {exc}"

    # 已有采样在跑就不叠加，等下一轮
    if not BOX_SAMPLE_LOCK.acquire(blocking=False):
        return

    def _runner() -> None:
        try:
            _worker()
        finally:
            try:
                BOX_SAMPLE_LOCK.release()
            except RuntimeError:
                pass

    threading.Thread(target=_runner, daemon=True, name="box-sample").start()


def _schedule_auth_refresh() -> None:
    """后台惰性验证 token + 拉取逐桩位真实数据，不阻塞 HTTP 请求。

    只在「尚未验证过」或「距上次刷新超过 DETAIL_REFRESH_SECONDS」时启动，
    且同一时刻最多一个刷新线程（AUTH_BG_LOCK 守卫）。
    """
    with AUTH_LOCK:
        never_checked = AUTH_STATE.get("checked_at") is None
        details_at = AUTH_STATE.get("details_at")
        already = bool(AUTH_STATE.get("refreshing"))
        stale = not details_at or (time.time() - float(details_at) >= DETAIL_REFRESH_SECONDS)
    if already or (not never_checked and not stale):
        return
    with AUTH_BG_LOCK:
        with AUTH_LOCK:
            if AUTH_STATE.get("refreshing"):
                return
            AUTH_STATE["refreshing"] = True

        def _worker() -> None:
            try:
                cfg = load_config()
                if not str(cfg.get("token") or "").strip():
                    return
                with AUTH_LOCK:
                    need_check = AUTH_STATE.get("checked_at") is None
                if need_check:
                    check_auth(force=True)
                if AUTH_STATE.get("valid"):
                    refresh_details(force=True)
            except Exception as exc:  # 后台任务不能让线程静默死掉
                with AUTH_LOCK:
                    AUTH_STATE["last_error"] = f"后台刷新失败: {exc}"
            finally:
                with AUTH_LOCK:
                    AUTH_STATE["refreshing"] = False

        threading.Thread(target=_worker, daemon=True, name="auth-refresh").start()


def auth_status() -> dict:
    cfg = load_config()
    token = str(cfg.get("token") or "").strip()
    with AUTH_LOCK:
        valid = bool(AUTH_STATE.get("valid")) and bool(token)
        details = AUTH_STATE.get("details") or {}
        real_ports = sum(len(v) for v in details.values())
        return {
            "mode": "auth" if token else "public",
            "token_configured": bool(token),
            "token_preview": ("*" * 8 + token[-4:]) if len(token) > 4 else ("*" * len(token)),
            "valid": valid,
            "reason": AUTH_STATE.get("reason") if token else "未配置 token",
            "checked_at": iso(AUTH_STATE.get("checked_at")),
            "mobile_masked": AUTH_STATE.get("mobile_masked") or "",
            "detail_active": valid and real_ports > 0,
            "detail_error": AUTH_STATE.get("last_error") if token else None,
            "detail_cabinets": len(details),
            "real_ports": real_ports,
            "details_at": iso(AUTH_STATE.get("details_at")),
            "refreshing": bool(AUTH_STATE.get("refreshing")),
        }


def _detail_snapshot(cabinet_no: str) -> list[dict]:
    with AUTH_LOCK:
        if not AUTH_STATE.get("valid"):
            return []
        return list((AUTH_STATE.get("details") or {}).get(cabinet_no) or [])


# ---------------------------------------------------------------- site mapping

_RAW_TO_SITE: dict[str, str] = {}
for _site in SITES_CFG["sites"]:
    for _raw in _site["raw_locations"]:
        _RAW_TO_SITE[_raw] = _site["site_id"]
SITE_BY_ID = {s["site_id"]: s for s in SITES_CFG["sites"]}
CAMPUS_MATCH = SITES_CFG["campus"]["location_match"]


def site_of(location) -> str | None:
    if not location:
        return None
    if location in _RAW_TO_SITE:
        return _RAW_TO_SITE[location]
    compact = "".join(str(location).split())
    for raw, site_id in _RAW_TO_SITE.items():
        if "".join(raw.split()) == compact:
            return site_id
    return None


def is_campus(location) -> bool:
    return bool(location) and CAMPUS_MATCH in str(location)


# ---------------------------------------------------------------- levels

def level_of(free: int, total: int) -> str:
    """按空闲率分级。离线由 online 字段单独表达，不在此混淆。"""
    if total <= 0 or free <= 0:
        return "none"
    ratio = free / total
    if ratio <= 0.15:
        return "low"
    if ratio <= 0.40:
        return "mid"
    return "high"


# ---------------------------------------------------------------- profiles

def load_profiles() -> None:
    try:
        raw = json.loads(PROFILE_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(raw, dict):
        PROFILES.update({k: v for k, v in raw.items() if isinstance(v, dict)})


def save_profiles() -> None:
    try:
        PROFILE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROFILE_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(PROFILES, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(PROFILE_CACHE)
    except OSError:
        pass


def warm_profiles(cabinet_nos: list[str]) -> None:
    """后台补充电柜档案（温度/信号/功率等）。失败不影响主流程。"""
    changed = False
    for cabinet_no in cabinet_nos:
        if cabinet_no in PROFILES:
            continue
        profile = tosun_client.cabinet_profile(cabinet_no)
        if profile:
            PROFILES[cabinet_no] = profile
            changed = True
        time.sleep(0.15)  # 对上游保持温和速率
    if changed:
        save_profiles()


# ---------------------------------------------------------------- polling

def poll_once(verbose: bool = False) -> dict:
    cfg = load_config()
    centers = [(c["lng"], c["lat"]) for c in SITES_CFG["campus"]["scan_centers"]]
    POLL_META["running"] = True
    POLL_META["last_attempt"] = time.time()
    try:
        raw = tosun_client.list_around(centers, float(cfg.get("scan_distance_km") or 3))

        rows: list[dict] = []
        items: list[dict] = []
        for cabinet_no, item in raw.items():
            if not is_campus(item.get("location")):
                continue
            rows.append(
                {
                    "cabinet_no": cabinet_no,
                    "chargeable": int(item.get("chargeable") or 0),
                    "capacity": tosun_client.capacity_total(item.get("capacity")),
                    "fault": int(item.get("errorCell") or 0),
                    "online": int(item.get("online") or 0) == 1,
                    "location": item.get("location"),
                    "cabinet_type": item.get("cabinetType"),
                    "lng": item.get("longitude"),
                    "lat": item.get("latitude"),
                }
            )
            items.append(item)

        stats = tracker.record_poll(rows)
        tracker.prune_impossible()

        price = tracker.get_kv("price_per_hour")
        if price is None:
            price = tosun_client.charge_per_hour()
            if price is not None:
                tracker.set_kv("price_per_hour", price)

        with STATE_LOCK:
            LAST_SNAPSHOT.clear()
            LAST_SNAPSHOT["items"] = items
            LAST_SNAPSHOT["at"] = POLL_META["last_attempt"]

        POLL_META["last_ok"] = time.time()
        POLL_META["last_error"] = None
        if verbose:
            print(
                f"[poll] {datetime.now(CST):%H:%M:%S} 校园电柜 {len(rows)} 台 · "
                f"新增占用 {stats['new_occupancy']} · 释放 {stats['released']}"
            )
        return {"ok": True, **stats}
    except Exception as exc:  # noqa: BLE001 - 采集失败必须记录而不能杀进程
        tracker.record_poll_failure(str(exc))
        POLL_META["last_error"] = str(exc)
        if verbose:
            print(f"[poll] 失败: {exc}")
        return {"ok": False, "error": str(exc)}
    finally:
        POLL_META["running"] = False


def poller_loop(stop: threading.Event) -> None:
    warmed = False
    while not stop.is_set():
        poll_once(verbose=True)
        if not warmed:
            nos = [i.get("cabinetNo") for i in (LAST_SNAPSHOT.get("items") or [])]
            nos = [n for n in nos if n]
            if nos:
                threading.Thread(target=warm_profiles, args=(nos,), daemon=True).start()
                warmed = True
        # 🔴 采集完成后立刻触发一次明细刷新，让「地图/概览」与「点位明细」
        # 在同一轮里同时更新。之前明细是 /api/state 被调用时才惰性触发，
        # 且限频 120 秒，导致明细总比汇总慢一拍（用户看到的就是"有更新延迟"）。
        # 这里改成采集驱动 + 限频 55 秒（略小于 60 秒轮询），两边节奏对齐。
        if str(load_config().get("token") or "").strip():
            _schedule_auth_refresh()
        stop.wait(max(MIN_POLL_GAP, int(load_config().get("poll_interval") or POLL_INTERVAL)))


# ---------------------------------------------------------------- aggregation

def iso(ts) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), CST).isoformat(timespec="seconds")


def _parse_epoch(value) -> float | None:
    """把平台时间串转成 Unix 秒。

    平台返回格式实测为 "2026-11-19 20:43:50"（无时区，本地时间 = CST）。
    也容忍 ISO 带 T / 带微秒 / 带时区偏移的写法，解析失败返回 None。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        # 平台若给毫秒时间戳，归一化到秒
        return v / 1000.0 if v > 1e11 else v
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    txt = s.replace("T", " ").split(".")[0]
    # 去掉可能存在的时区尾巴（+08:00 / Z）
    for tail in ("+08:00", "+0800", "Z"):
        if txt.endswith(tail):
            txt = txt[: -len(tail)].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(txt, fmt).replace(tzinfo=CST).timestamp()
        except ValueError:
            continue
    return None


def _build_real_occupancy(
    caboxs: list[dict], samples: list[float] | None = None, cabinet_no: str = "", online: bool = True
) -> dict:
    """把平台返回的真实 caboxs[] 映射为桩位卡片。

    实测（2026-11-19）关键修正：
      * chargeTime 字段恒为 null，不能用。
      * 占用判据不能只看 chargeOrderNo 前缀；必须是 chargerStatus ∈ {1,2,3}，
        否则历史残留订单（CD... 但 status=0、endTime 是昨天）会被误判为占用。

    时长模型（本项目核心增量，平台无倒计时字段）：
      * 已充时长：固件 ct（精确，与 startTime 差 <3min）> endTime-startTime > 统计
      * 目标时长：cs=6 时 ct 冻结在目标上可精确反推；cs=3 且 ct≥420 时必为 480
      * 剩余时长：目标已知则精确；未知则给出「≤480-ct」的**精确上界**
      * 平台把 cs=6（到点断电未取走）当空闲展示 → 单独标记 physically_blocked，
        避免用户看到"空闲"跑过去发现电池还占着
    """
    # 🔴 离线电柜必须和 _build_occupancy 保持一致：不返回桩位列表。
    # 否则离线柜仍会吐出 capacity 个 platform_real 桩位，而 stats 只统计在线柜
    # （ports_total 用 online 柜的 capacity 之和），造成「桩位列表 624 vs stats 584」
    # 这种跨模块数字不一致 —— 用户看到的就是"各模块不同步"。
    if not online:
        return {
            "estimated": True,
            "offline": True,
            "ports": [],
            "note": "电柜离线，最后一次读数不作为当前桩位状态",
        }

    pool = samples or []
    now = datetime.now(CST)
    ports = []
    for cabox in caboxs:
        if not isinstance(cabox, dict):
            continue
        status = cabox.get("status")
        try:
            status_int = int(status) if status not in (None, "") else 1
        except (TypeError, ValueError):
            status_int = 1

        try:
            port_no = int(cabox.get("port")) if cabox.get("port") not in (None, "") else None
        except (TypeError, ValueError):
            port_no = None

        box = {}
        if port_no is not None and cabinet_no:
            box = box_sampler.cached(cabinet_no, port_no) or {}

        # 🔴 统一口径（见 tosun_client.classify_port）：
        #   free     = infraed==0 且 status==1  ← 与平台 chargeable 同义
        #   occupied = infraed==1 且充电中
        #   blocked  = infraed==1 但未充电（到点断电/手动停止，电池仍插着）
        #   fault    = status!=1
        # 这样明细的 free 计数恒等于地图/平台 chargeable，两者不再不同步。
        state = tosun_client.port_state_of(cabox, box)
        occupied = state == tosun_client.PORT_OCCUPIED
        blocked = state == tosun_client.PORT_BLOCKED

        elapsed = None
        remaining = None
        remaining_max = None
        target_minutes = None
        min_target = None
        estimate_basis = None
        remaining_is_upper = False
        is_exact = False
        target_prob = None
        end_at = None
        plan = None
        bound = None

        if state in ("occupied", "blocked"):
            # 已充时长：固件 ct 最准（与 startTime 差 <3min）
            if box and box.get("ct") is not None:
                elapsed = round(float(box["ct"]), 1)
                estimate_basis = "firmware_ct"
            else:
                elapsed = tosun_client.charge_elapsed_minutes(cabox, now)
                if elapsed is not None:
                    estimate_basis = "endtime_minus_starttime"

            # 目标时长反推 → 结束时刻 → 剩余时长
            start_epoch = _parse_epoch(cabox.get("startTime"))
            plan = target_model.plan(box, start_epoch) if box else None
            if plan:
                target_minutes = plan["target_minutes"]
                target_prob = plan["target_probability"]
                is_exact = plan["target_exact"]
                remaining = plan["remaining_minutes"]
                remaining_max = plan["remaining_minutes"]
                end_at = plan["end_at"]
                remaining_is_upper = not is_exact
                estimate_basis = plan["source"]
                # 硬上界：即使目标推断错了，480 分钟必定断电
                if not is_exact and elapsed is not None:
                    remaining_max = round(
                        max(0.0, tosun_client.MAX_CHARGE_MINUTES - float(elapsed)), 1
                    )
            else:
                bound = tosun_client.remaining_bounds(box)
                if bound:
                    remaining = bound["remaining_min_minutes"]
                    remaining_max = bound["remaining_max_minutes"]
                    is_exact = bound["is_exact"]
                    remaining_is_upper = not is_exact
                    target_minutes = bound["target_minutes"]
                    min_target = bound["min_target_minutes"]
                    estimate_basis = (
                        "firmware_target" if is_exact
                        else ("firmware_stopped" if bound["stopped"] else "firmware_slot_bound")
                    )
                elif elapsed is not None and pool:
                    # 没采到固件：退回历史统计（明确标注为推算）
                    remaining = tracker.estimate_remaining(pool, elapsed)["remaining_minutes"]
                    estimate_basis = (
                        "conditional_median" if len(pool) >= 8 else "exponential_prior"
                    )
                    remaining_is_upper = True

        ports.append({
            "port": port_no if port_no is not None else len(ports) + 1,
            "cabox_id": cabox.get("caboxId"),
            "state": state,
            "source": "platform_real",
            "elapsed_minutes": elapsed,
            "elapsed_is_lower_bound": False,
            # 剩余区间 [remaining_minutes, remaining_max_minutes]
            "remaining_minutes": remaining,
            "remaining_max_minutes": remaining_max,
            "remaining_is_upper_bound": remaining_is_upper,
            "remaining_is_exact": is_exact,
            "min_target_minutes": min_target,
            "target_minutes": target_minutes,
            "target_probability": target_prob,
            "target_candidates": plan["target_candidates"] if plan else None,
            "end_at": end_at,
            "estimate_basis": estimate_basis,
            # cs=6：已断电但电池未取走，平台算空闲、实际插不进去
            "physically_blocked": blocked,
            "charger_status": cabox.get("chargerStatus"),
            "charger_status_text": tosun_client.cabox_charger_status_text(cabox.get("chargerStatus")),
            "firmware_status": box.get("cs") if box else None,
            "firmware_status_text": box.get("cs_text") if box else None,
            "voltage": box.get("voltage") if box else cabox.get("voltage"),
            "ampere": box.get("ampere") if box else cabox.get("ampere"),
            "energy_kwh": box.get("energy_kwh"),
            "start_time": cabox.get("startTime"),
            "end_time": cabox.get("endTime"),
            "battery_type": cabox.get("batteryType"),
            "percentage": cabox.get("percentage"),
            "infraed": cabox.get("infraed"),
        })

    order = {"occupied": 0, "blocked": 1, "free": 2, "fault": 3}
    ports.sort(key=lambda p: (order.get(p["state"], 9), p["port"] or 0))
    precise = sum(1 for p in ports if p.get("remaining_is_exact"))
    bounded = sum(1 for p in ports if p.get("remaining_is_upper_bound"))
    blocked_n = sum(1 for p in ports if p.get("physically_blocked"))
    return {
        "estimated": False,
        "source": "platform_real",
        "ports": ports,
        "precise_remaining": precise,
        "bounded_remaining": bounded,
        "physically_blocked": blocked_n,
        "note": (
            "已充时长取桩端固件 ct（精确，与 startTime 差 <3min）；"
            "剩余时长受「目标只能取离散档位且最长 480 分钟」约束，"
            "给出有保证的区间 [最快, 最慢]，ct≥420 时收敛为精确值"
        ),
    }


def _make_cabinet(
    item: dict,
    db_row: dict | None,
    active_starts: list[dict],
    median,
    samples: list[float] | None = None,
    detail_caboxs: list[dict] | None = None,
) -> dict:
    cabinet_no = item["cabinetNo"]
    split = tosun_client.capacity_split(item.get("capacity"))
    total = sum(split)
    free = int(item.get("chargeable") or 0)
    fault = min(int(item.get("errorCell") or 0), total)
    occupied = max(0, total - free - fault)
    online = int(item.get("online") or 0) == 1
    has_real = bool(detail_caboxs)

    occupancy = (
        _build_real_occupancy(detail_caboxs, samples, cabinet_no, online)
        if has_real
        else _build_occupancy(total, free, occupied, fault, active_starts, median, samples, online)
    )

    # 🔴 有明细时，各分类计数一律从**已构建的桩位列表**数出来，
    # 保证「概览/地图/点位明细」三处用的是同一份数据，不会互相打架。
    #
    # 口径（见 tosun_client.classify_port）：
    #   free     = infraed==0 且 status==1   ← 与平台 chargeable **恒等**
    #   blocked  = infraed==1 但未充电（电池插着，插不进去）
    #   fault    = status!=1
    #   occupied = 其余（infraed==1 且正在充电）
    #
    # `free` 直接用平台 chargeable（每 60 秒新鲜、且与小程序地图一致）；
    # `blocked`/`fault` 从明细桩位数出来；`occupied` 由 capacity 反推 ——
    # 四类之和恒等于 capacity，且两次快照之间的自然变化自动落在 occupied 上。
    blocked = 0
    if has_real:
        port_states = [p.get("state") for p in (occupancy.get("ports") or [])]
        blocked = port_states.count(tosun_client.PORT_BLOCKED)
        fault = port_states.count(tosun_client.PORT_FAULT)
        occupied = max(0, total - free - blocked - fault)
    elif not online:
        # 离线柜：不返回桩位，也不贡献任何计数（与 stats 只统计在线柜一致）
        free = occupied = fault = blocked = 0

    return {
        "cabinet_no": cabinet_no,
        "site_id": site_of(item.get("location")),
        "location": item.get("location"),
        "cabinet_type": item.get("cabinetType"),
        "type_label": tosun_client.type_label(item.get("cabinetType")),
        "capacity": total,
        "capacity_raw": item.get("capacity"),
        "capacity_split": split,
        "chargeable": free,
        "occupied": occupied,
        "blocked": blocked,
        "fault": fault,
        "exchangeable": int(item.get("exchangeable") or 0),
        "online": online,
        "cabinet_status": item.get("cabinetStatus"),
        "free_ratio": round(free / total, 4) if total else 0.0,
        "lng": item.get("longitude"),
        "lat": item.get("latitude"),
        "observed_at": iso(db_row.get("observed_at")) if db_row else None,
        "changed_at": iso(db_row.get("changed_at")) if db_row else None,
        "profile_summary": _profile_summary(cabinet_no),
        # 离线电柜的最后一次读数不可信：既不能把旧 chargeable 当成"现在还有 N 空位"，
        # 也不能把旧的占用数展开成带时长的桩位。统一降级为 unknown。
        "level": level_of(free, total) if online else "offline",
        "detail_source": "platform_real" if has_real else "estimated",
        "occupancy": occupancy,
    }


def _profile_summary(cabinet_no: str) -> dict | None:
    profile = PROFILES.get(cabinet_no)
    if not profile:
        return None
    return {
        "belong_user": profile.get("belongUser"),
        "agent_name": profile.get("agentName"),
        "amper": profile.get("amper"),
        "cab_temp": profile.get("cabTemp"),
        "rssi": profile.get("rssi"),
        "hw": profile.get("hw"),
        "fw": profile.get("fw"),
        "active_date": profile.get("activeDate"),
        "expiry_date": profile.get("expiryDate"),
    }


def _build_occupancy(
    total: int,
    free: int,
    occupied: int,
    fault: int,
    active_starts: list[dict],
    median,
    samples: list[float] | None = None,
    online: bool = True,
) -> dict:
    """把「有 N 个占用中」展开成逻辑桩位。桩位号是逻辑编号，非物理 port。

    active_starts 每项 {started_at, basis}：
      observed → elapsed 准确
      backlog  → 只知"至少已充这么久"，elapsed 是下界

    剩余时间统一用 tracker.estimate_remaining() 做条件化估计，而不是
    朴素的 median - elapsed（后者在超时后会给 0，失真明显）。
    samples 由调用方一次取出后复用，避免逐桩位查库。
    """
    if not online:
        # 离线电柜的 chargeable 是历史残值，据此展开桩位会给出虚假的"已充 N 分钟"
        return {
            "estimated": True,
            "offline": True,
            "ports": [],
            "note": "电柜离线，最后一次读数不作为当前桩位状态",
        }

    now = time.time()
    pool = samples or []
    # 先排 known（有准确起点），再排 backlog（下界），信息量大的优先展示
    entries = sorted(
        active_starts[-occupied:] if occupied else [],
        key=lambda e: (e.get("basis") != "observed", e["started_at"]),
    )

    ports = []
    for index in range(occupied):
        entry = entries[index] if index < len(entries) else None
        if entry is None:
            # 有占用数但队列没记录（例如刚启动尚未补齐）
            ports.append(
                {
                    "port": index + 1,
                    "state": "occupied",
                    "elapsed_minutes": None,
                    "elapsed_is_lower_bound": False,
                    "remaining_minutes": None,
                    "remaining_is_upper_bound": False,
                    "median_minutes": round(float(median), 1) if median else None,
                    "basis": "unknown",
                }
            )
            continue

        elapsed = round((now - entry["started_at"]) / 60.0, 1)
        is_backlog = entry.get("basis") == "backlog"
        estimate = tracker.estimate_remaining(pool, elapsed)
        remaining = estimate["remaining_minutes"]
        if is_backlog and remaining is not None:
            remaining = round(min(remaining, max(0.0, float(median or 0) - elapsed)), 1)

        ports.append(
            {
                "port": index + 1,
                "state": "occupied",
                "elapsed_minutes": elapsed,
                # backlog 的起点只是"首次看见"，真实充电时间只会更长
                "elapsed_is_lower_bound": is_backlog,
                "remaining_minutes": remaining,
                # backlog 起点未知 → 剩余时间只能是上界
                "remaining_is_upper_bound": is_backlog,
                "median_minutes": round(float(median), 1) if median else None,
                "basis": entry.get("basis"),
                "estimate_basis": estimate["basis"],
            }
        )

    for index in range(free):
        ports.append(
            {
                "port": occupied + index + 1,
                "state": "free",
                "elapsed_minutes": None,
                "elapsed_is_lower_bound": False,
                "remaining_minutes": 0,
                "remaining_is_upper_bound": False,
                "median_minutes": round(float(median), 1) if median else None,
                "basis": "observed",
            }
        )
    for index in range(fault):
        ports.append(
            {
                "port": occupied + free + index + 1,
                "state": "fault",
                "elapsed_minutes": None,
                "elapsed_is_lower_bound": False,
                "remaining_minutes": None,
                "remaining_is_upper_bound": False,
                "median_minutes": None,
                "basis": "derived",
            }
        )
    return {
        "estimated": True,
        "ports": ports,
        "note": "桩位号为逻辑编号，按空闲数变化顺序推定，非真实物理编号",
    }


def _build_forecast(
    nos: list[str], stats: dict, active: dict[str, list[dict]], samples: list[float] | None = None
) -> dict:
    now = time.time()
    median = stats.get("median_minutes")
    pool = samples or []

    # 只对"看见开始"的占用做剩余时间预测；backlog 的起点未知，不参与
    pendings = [
        (now - entry["started_at"]) / 60.0
        for n in nos
        for entry in active.get(n, [])
        if entry.get("basis") == "observed"
    ]

    out = {
        "median_charge_minutes": median,
        "p25_minutes": stats.get("p25_minutes"),
        "p75_minutes": stats.get("p75_minutes"),
        "samples": stats.get("samples", 0),
        "confidence": stats.get("confidence", "none"),
        "tracked_occupancies": len(pendings),
        "expected_free_15m": None,
        "expected_free_30m": None,
        "expected_free_60m": None,
        "next_free_in_minutes": None,
    }
    if median is None or not pendings:
        return out

    # 与桩位卡片一致的算法，保证"点位预测"和"单桩剩余"不会互相矛盾
    remaining = [tracker.estimate_remaining(pool, e)["remaining_minutes"] for e in pendings]
    out["expected_free_15m"] = sum(1 for r in remaining if r <= 15)
    out["expected_free_30m"] = sum(1 for r in remaining if r <= 30)
    out["expected_free_60m"] = sum(1 for r in remaining if r <= 60)
    out["next_free_in_minutes"] = round(min(remaining), 1) if remaining else None
    return out


def _resolve_items() -> list[dict]:
    with STATE_LOCK:
        items = list(LAST_SNAPSHOT.get("items") or [])
    if items:
        return items
    return _items_from_db()


def _items_from_db() -> list[dict]:
    """服务刚启动、尚未完成首次采集时，用 DB 最近状态兜底。"""
    out = []
    for row in tracker.all_cabinet_states():
        if not row.get("location"):
            continue
        out.append(
            {
                "cabinetNo": row["cabinet_no"],
                "location": row["location"],
                "chargeable": row["chargeable"],
                "capacity": str(row["capacity"]),
                "errorCell": row["fault"],
                "exchangeable": row["exchangeable"],
                "online": row["online"],
                "cabinetType": row["cabinet_type"] or 3,
                "cabinetStatus": 1,
                "longitude": row["lng"],
                "latitude": row["lat"],
            }
        )
    return out


def build_state() -> dict:
    cfg = load_config()
    items = _resolve_items()
    all_nos = [i["cabinetNo"] for i in items]

    # 配置了 token 时惰性验证并刷新逐桩位真实数据。
    # 逐桩位接口很重（实测 48 柜约 76s），绝不能阻塞 /api/state（会超时）；
    # 改为后台线程刷新，前端首屏先用推算数据，真实数据到位后下一次轮询自动切换。
    if str(cfg.get("token") or "").strip():
        _schedule_auth_refresh()

    active = tracker.active_occupancies(all_nos)
    global_stats = tracker.duration_stats(all_nos)
    states = {r["cabinet_no"]: r for r in tracker.all_cabinet_states()}

    by_site: dict[str, list[dict]] = {}
    for item in items:
        site_id = site_of(item.get("location"))
        if not site_id:
            continue
        by_site.setdefault(site_id, []).append(item)

    sites = []
    totals = {
        "cabinets": 0,
        "cabinets_all": 0,
        "cabinets_offline": 0,
        "ports_total": 0,
        "ports_free": 0,
        "ports_occupied": 0,
        "ports_fault": 0,
        "ports_blocked": 0,
        "ports_precise": 0,
    }

    for site_id, site_items in by_site.items():
        cfg_site = SITE_BY_ID[site_id]
        nos = [i["cabinetNo"] for i in site_items]

        site_stats = tracker.duration_stats(nos)
        if site_stats["confidence"] == "none":
            site_stats = global_stats
        median = site_stats.get("median_minutes")

        # 一次取出本点位的历史样本，供所有桩位复用（不足 8 条回退全校区池）
        site_samples = tracker.session_minutes(nos)
        sample_scope = site_id
        if len(site_samples) < 8:
            site_samples = tracker.session_minutes(all_nos)
            sample_scope = "campus"

        cabinets = [
            _make_cabinet(
                item,
                states.get(item["cabinetNo"]),
                active.get(item["cabinetNo"], []),
                median,
                site_samples,
                _detail_snapshot(item["cabinetNo"]),
            )
            for item in site_items
        ]

        # 离线电柜的 chargeable 是历史残值，不能混进"现在有多少空位"的汇总
        live = [c for c in cabinets if c["online"]]
        ports_total = sum(c["capacity"] for c in live)
        ports_free = sum(c["chargeable"] for c in live)
        ports_occupied = sum(c["occupied"] for c in live)
        ports_fault = sum(c["fault"] for c in live)
        # cs=6（到点断电、电池未取走）平台算空闲，但物理上插不进去。
        # 从"真正可用空位"里扣掉，单独计数，避免用户白跑。
        ports_blocked = sum(
            1
            for c in live
            for p in (c.get("occupancy") or {}).get("ports", [])
            if p.get("physically_blocked")
        )
        # 🔴 口径统一后：`blocked`（infraed==1 但未充电）的端口**本来就不在**
        # 平台 `chargeable` 里，所以 ports_free 已经是"真正能插进去的空位"，
        # 不能再减一次 blocked（早期实现减了，导致明细比地图少算一截）。
        ports_free_real = ports_free
        ports_precise = sum(
            (c.get("occupancy") or {}).get("precise_remaining") or 0 for c in live
        )
        online_count = len(live)
        offline_count = len(cabinets) - online_count

        lngs = [float(c["lng"]) for c in cabinets if c.get("lng") is not None]
        lats = [float(c["lat"]) for c in cabinets if c.get("lat") is not None]
        center = cfg_site["center"]

        sites.append(
            {
                "site_id": site_id,
                "name": cfg_site["name"],
                "short": cfg_site.get("short"),
                "raw_locations": cfg_site["raw_locations"],
                "center": center,
                "bounds": {
                    "minLng": min(lngs) if lngs else center["lng"],
                    "maxLng": max(lngs) if lngs else center["lng"],
                    "minLat": min(lats) if lats else center["lat"],
                    "maxLat": max(lats) if lats else center["lat"],
                },
                "stats": {
                    "cabinets": len(cabinets),
                    "cabinets_online": online_count,
                    "cabinets_offline": offline_count,
                    "ports_total": ports_total,
                    "ports_free": ports_free,
                    # 真正可用的空位 = 平台说空闲 - 电池还占着的（cs=6）
                    "ports_free_real": ports_free_real,
                    "ports_blocked": ports_blocked,
                    "ports_occupied": ports_occupied,
                    "ports_fault": ports_fault,
                    "ports_precise": ports_precise,
                    "online": online_count,
                    "free_ratio": round(ports_free_real / ports_total, 4) if ports_total else 0.0,
                    "occupancy_ratio": round(ports_occupied / ports_total, 4)
                    if ports_total
                    else 0.0,
                },
                "level": level_of(ports_free_real, ports_total) if online_count else "offline",
                # 显式的点位级离线标记：前端不必靠"所有电柜都 online=false"去猜
                "offline": online_count == 0,
                "cabinets": sorted(cabinets, key=lambda c: c["cabinet_no"]),
                "forecast": _build_forecast(nos, site_stats, active, site_samples),
                "sample_scope": sample_scope,
            }
        )

        totals["cabinets"] += len(live)
        totals["cabinets_all"] += len(cabinets)
        totals["cabinets_offline"] += offline_count
        totals["ports_total"] += ports_total
        totals["ports_free"] += ports_free
        totals["ports_occupied"] += ports_occupied
        totals["ports_fault"] += ports_fault
        totals["ports_blocked"] += ports_blocked
        totals["ports_precise"] += ports_precise

    sites.sort(key=lambda s: (-s["stats"]["ports_total"], s["name"]))

    last_ok = POLL_META["last_ok"] or tracker.get_kv("last_poll_at")
    age = time.time() - float(last_ok) if last_ok else None
    interval = max(MIN_POLL_GAP, int(cfg.get("poll_interval") or POLL_INTERVAL))

    return {
        "server_time": datetime.now(CST).isoformat(timespec="seconds"),
        "server_epoch_ms": int(time.time() * 1000),
        "mode": "auth" if cfg.get("token") else "public",
        "freshness": {
            "interval_seconds": interval,
            "last_poll_at": iso(last_ok),
            "last_poll_ok": POLL_META["last_error"] is None and last_ok is not None,
            "next_poll_in_seconds": max(0, round(interval - (age or interval))),
            "stale": age is None or age > STALE_AFTER,
            "age_seconds": round(age, 1) if age is not None else None,
            "last_error": POLL_META["last_error"],
        },
        "stats": {
            **totals,
            # blocked 端口不在 chargeable 里，free 已是真可用空位，不再重复扣减
            "ports_free_real": totals["ports_free"],
            "sites": len(sites),
            "free_ratio": round(totals["ports_free"] / totals["ports_total"], 4)
            if totals["ports_total"]
            else 0.0,
        },
        "price": {
            "per_hour": tracker.get_kv("price_per_hour"),
            "currency": "CNY",
            "source": "getChargePerHour",
        },
        "duration_model": {
            **global_stats,
            "prior_minutes": tracker.PRIOR_MINUTES,
            "max_charge_minutes": tosun_client.MAX_CHARGE_MINUTES,
            "certain_target_minutes": tosun_client.TARGET_CERTAIN_MINUTES,
            "note": (
                "已充时长取桩端固件 ct；目标时长在 cs=6（到点断电）或 ct≥420 时可确定，"
                "此时剩余为精确值，否则为「不超过 480-ct」的上界。"
                "历史统计仅在未采到固件时兜底。"
            ),
        },
        "firmware": box_sampler.stats(),
        "target_distribution": target_model.distribution(),
        "auth": auth_status(),
        "sites": sites,
    }


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "SysuChargeWatch/1.0"
    protocol_version = "HTTP/1.1"
    api = None  # main() 里注入

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_json(404, {"error": "not_found", "file": path.name})
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in (".js", ".mjs"):
            ctype = "text/javascript"
        if ctype.startswith("text/") or ctype == "text/javascript":
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if route == "/":
            route = "/index.html"

        if route.startswith("/api/"):
            status, payload = self.api.handle_get(route, query)
            self._send_json(status, payload)
            return

        candidate = (WEB / route.lstrip("/")).resolve()
        try:
            candidate.relative_to(WEB.resolve())
        except ValueError:
            self._send_json(403, {"error": "forbidden"})
            return
        self._send_file(candidate)

    def do_POST(self) -> None:  # noqa: N802
        route = unquote(urlparse(self.path).path)
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid_json"})
            return
        status, payload = self.api.handle_post(route, body)
        self._send_json(status, payload)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.send_header("access-control-allow-headers", "content-type")
        self.send_header("content-length", "0")
        self.end_headers()


def main() -> int:
    parser = argparse.ArgumentParser(description="SYSU Charge Watch 本地看板")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--once", action="store_true", help="只采集一次后退出")
    parser.add_argument("--no-poll", action="store_true", help="不启动后台采集")
    args = parser.parse_args()

    tracker.init_db()
    load_profiles()

    if args.once:
        result = poll_once(verbose=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    Handler.api = Api(sys.modules[__name__])

    # 先绑定端口再采集：首次网络请求可能耗时数秒，不能让监听被阻塞。
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    stop = threading.Event()
    if not args.no_poll:

        def _first_poll() -> None:
            poll_once(verbose=True)
            poller_loop(stop)

        threading.Thread(target=_first_poll, daemon=True).start()

    print(f"SYSU Charge Watch 已启动: http://{args.host}:{args.port}/", flush=True)
    if args.host == "0.0.0.0":
        try:
            import socket as _s
            _sct = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            _sct.connect(("8.8.8.8", 80))
            _lan = _sct.getsockname()[0]
            _sct.close()
            print(f"  局域网地址（手机用同一 WiFi 访问）: http://{_lan}:{args.port}/", flush=True)
        except Exception:
            print("  局域网地址获取失败，请用 ipconfig 查本机 WLAN 的 IPv4 地址。", flush=True)
    print(f"  数据源 {tosun_client.BASE}{tosun_client.EP_LIST_AROUND}（免登录只读）")
    print(f"  数据库 {tracker.DB_PATH}")
    print(f"  采集间隔 {load_config().get('poll_interval')}s · Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        stop.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
