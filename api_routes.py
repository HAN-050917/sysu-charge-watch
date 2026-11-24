"""SYSU Charge Watch — HTTP 接口层。

从 server.py 拆分出来，避免主文件过长；由 server.py 注入 server 模块本身。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8))


def iso(ts) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), CST).isoformat(timespec="seconds")


class Api:
    """把 server 模块的能力包装成接口对象（便于单测与复用）。"""

    def __init__(self, server_module):
        self.srv = server_module

    # ------------------------------------------------ GET
    def handle_get(self, route: str, query: dict[str, list[str]]) -> tuple[int, Any]:
        srv = self.srv
        if route == "/api/state":
            return 200, srv.build_state()
        if route == "/api/meta":
            return 200, self.meta()
        if route == "/api/health":
            return 200, self.health()
        if route == "/api/config":
            return 200, self.safe_config()
        if route == "/api/auth":
            return 200, self.srv.auth_status()
        if route == "/api/sites":
            return 200, {
                "campus": srv.SITES_CFG["campus"],
                "sites": srv.SITES_CFG["sites"],
                "cabinet_types": srv.SITES_CFG["cabinet_types"],
                "levels": srv.SITES_CFG["levels"],
            }
        if route.startswith("/api/site/"):
            return self.site(route[len("/api/site/"):])
        if route.startswith("/api/cabinet/"):
            return self.cabinet(route[len("/api/cabinet/"):])
        if route.startswith("/api/history/"):
            cabinet_no = route[len("/api/history/"):]
            hours = 24
            if query.get("hours"):
                try:
                    hours = max(1, min(720, int(query["hours"][0])))
                except (TypeError, ValueError):
                    pass
            return self.history(cabinet_no, hours)
        if route == "/api/site-history":
            site_id = query.get("site", [None])[0] or "all"
            hours = 24
            if query.get("hours"):
                try:
                    hours = max(1, min(720, int(query["hours"][0])))
                except (TypeError, ValueError):
                    pass
            # day=YYYY-MM-DD → 自然日窗口（0:00–23:59）；不给就是滚动 hours 窗口
            day = (query.get("day", [None])[0] or "").strip() or None
            return self.site_history(site_id, hours, day)
        if route == "/api/history-days":
            site_id = query.get("site", [None])[0] or "all"
            days = 7
            if query.get("days"):
                try:
                    days = max(1, min(31, int(query["days"][0])))
                except (TypeError, ValueError):
                    pass
            return self.history_days(site_id, days)
        return 404, {"error": "not_found", "route": route}

    # ------------------------------------------------ POST
    def handle_post(self, route: str, body: dict[str, Any]) -> tuple[int, Any]:
        srv = self.srv
        if route == "/api/poll":
            now = time.time()
            last = srv.POLL_META.get("last_attempt")
            if last and now - last < srv.MIN_POLL_GAP:
                return 429, {
                    "ok": False,
                    "reason": "rate_limited",
                    "retry_in_seconds": round(srv.MIN_POLL_GAP - (now - last), 1),
                }
            result = srv.poll_once(verbose=False)
            return (200 if result.get("ok") else 502), result

        if route == "/api/config":
            patch = body if isinstance(body, dict) else {}
            allowed = {
                k: v
                for k, v in patch.items()
                if k in ("poll_interval", "token", "mobile", "scan_distance_km")
            }
            token_changed = "token" in allowed
            mobile_changed = "mobile" in allowed
            if "mobile" in allowed:
                allowed["mobile"] = str(allowed["mobile"] or "").strip()
            if token_changed:
                new_token = str(allowed["token"] or "").strip()
                allowed["token"] = new_token
            if token_changed or mobile_changed:
                # token 或手机号变化时立即作废缓存，下次 build_state 会重新验证
                with self.srv.AUTH_LOCK:
                    self.srv.AUTH_STATE.update({
                        "valid": None, "reason": None, "checked_at": None,
                        "mobile": "", "mobile_masked": "", "user": {}, "details": {},
                        "errors": {}, "details_at": None, "last_error": None,
                    })
            if "poll_interval" in allowed:
                try:
                    allowed["poll_interval"] = max(
                        srv.MIN_POLL_GAP, min(600, int(allowed["poll_interval"]))
                    )
                except (TypeError, ValueError):
                    allowed.pop("poll_interval")
            if "scan_distance_km" in allowed:
                # 单位是公里：1 km 只覆盖单个片区，50 km 会撞上服务端 100 条上限
                try:
                    allowed["scan_distance_km"] = max(
                        0.5, min(50.0, float(allowed["scan_distance_km"]))
                    )
                except (TypeError, ValueError):
                    allowed.pop("scan_distance_km")
            if "token" in allowed:
                allowed["token"] = str(allowed["token"] or "").strip()
            srv.save_config(allowed)
            response = self.safe_config()
            if (token_changed or mobile_changed) and (allowed.get("token") or allowed.get("mobile")):
                # 保存即验证一次，让前端马上拿到有效/失效结论
                response["auth_check"] = self.srv.check_auth(force=True)
            return 200, response

        if route == "/api/auth/check":
            force = bool(body.get("force")) if isinstance(body, dict) else False
            checked = self.srv.check_auth(force=force)
            if checked.get("valid"):
                # 逐桩位真实数据很重（实测 48 柜约 76s），后台异步拉，
                # 不阻塞按钮请求；前端下次轮询 /api/state 时自动拿到。
                self.srv._schedule_auth_refresh()
                checked.update(self.srv.auth_status())
            return 200, checked

        if route == "/api/auth/details":
            force = bool(body.get("force")) if isinstance(body, dict) else False
            # 手动强制刷新也走后台，立即返回「已开始」
            self.srv._schedule_auth_refresh()
            return 200, {"ok": True, "scheduled": True, "auth": self.srv.auth_status()}

        return 404, {"error": "not_found", "route": route}

    # ------------------------------------------------ 处理器
    def meta(self) -> dict:
        srv = self.srv
        return {
            "campus": srv.SITES_CFG["campus"],
            "sites": [
                {
                    "site_id": s["site_id"],
                    "name": s["name"],
                    "short": s.get("short"),
                    "center": s["center"],
                    "raw_locations": s["raw_locations"],
                }
                for s in srv.SITES_CFG["sites"]
            ],
            "cabinet_types": srv.SITES_CFG["cabinet_types"],
            "levels": srv.SITES_CFG["levels"],
            "data_source": {
                "name": "青春号 TOSUN",
                "endpoint": "https://cloud.tosunelectric.com/v1/user/getCabinetListAround",
                "auth_required": False,
                "granularity": "per-cabinet",
                "note": "仅汇总数；逐桩位明细需登录，本项目不登录",
            },
            "disclaimer": (
                "本页面数据来自青春号 TOSUN 公开的附近电柜查询接口（getCabinetListAround），"
                "仅做只读聚合与展示，不涉及登录、账户、支付、订单或启停充电。"
                "「已充时长 / 剩余时长」为基于空闲数变化的历史推算，非平台官方数据，仅供参考。"
            ),
        }

    def health(self) -> dict:
        srv = self.srv
        return {
            "ok": True,
            "app": "sysu-charge-watch",
            **srv.tracker.health(),
            "poll": {
                "last_attempt": iso(srv.POLL_META.get("last_attempt")),
                "last_ok": iso(srv.POLL_META.get("last_ok")),
                "last_error": srv.POLL_META.get("last_error"),
                "running": srv.POLL_META.get("running"),
            },
        }

    def safe_config(self) -> dict:
        cfg = self.srv.load_config()
        token = str(cfg.get("token") or "")
        mobile = str(cfg.get("mobile") or "")
        return {
            "poll_interval": cfg.get("poll_interval"),
            "scan_distance_km": cfg.get("scan_distance_km"),
            "mobile_configured": bool(mobile),
            "mobile_preview": self.srv._mask_mobile(mobile) if mobile else "",
            "token_configured": bool(token),
            "token_preview": ("*" * 8 + token[-4:]) if len(token) > 4 else ("*" * len(token)),
            "mode": "auth" if token else "public",
            "auth": self.srv.auth_status(),
        }

    def site(self, site_id: str) -> tuple[int, Any]:
        for site in self.srv.build_state()["sites"]:
            if site["site_id"] == site_id:
                return 200, site
        return 404, {"error": "site_not_found", "site_id": site_id}

    def cabinet(self, cabinet_no: str) -> tuple[int, Any]:
        for site in self.srv.build_state()["sites"]:
            for cabinet in site["cabinets"]:
                if cabinet["cabinet_no"] == cabinet_no:
                    profile = self.srv.PROFILES.get(cabinet_no)
                    if profile is None:
                        profile = self.srv.tosun_client.cabinet_profile(cabinet_no)
                        if profile:
                            self.srv.PROFILES[cabinet_no] = profile
                            self.srv.save_profiles()
                    return 200, {**cabinet, "site_name": site["name"], "profile": profile}
        return 404, {"error": "cabinet_not_found", "cabinet_no": cabinet_no}

    def history(self, cabinet_no: str, hours: int) -> tuple[int, Any]:
        points = self.srv.tracker.series(cabinet_no, hours)
        return 200, {
            "cabinet_no": cabinet_no,
            "hours": hours,
            "count": len(points),
            "points": [
                {
                    "t": iso(p["at"]),
                    "chargeable": p["chargeable"],
                    "capacity": p["capacity"],
                    "online": p["online"],
                }
                for p in points
            ],
        }

    def site_history(self, site_id: str, hours: int,
                     day: str | None = None) -> tuple[int, Any]:
        """某点位（或全校区）的可用/占用桩位聚合时序。

        两种窗口：
        - **自然日**（`day=YYYY-MM-DD`，前端日期栏用）：窗口固定为当天
          `[00:00, 24:00)`，X 轴恒为 0:00–23:59，缺数据的时段直接没有点，
          曲线不会被拉伸；
        - **滚动窗口**（不给 day）：最近 `hours` 小时，边界随当前时刻移动。

        数据来自 cabinet_sample 原始采样：每个采样时刻，把该点位所有在线电柜的
        chargeable 之和作为「可用」，capacity - chargeable - fault 之和作为「占用」。
        fault 数不在这张表里，用当前 cabinet_state 的 fault 作为近似（故障数很少变动）。
        按 10 分钟分桶，每柜每桶只取最后一条样本，避免 60s 级原始数据把数字放大 10 倍。
        """
        srv = self.srv
        # 自然日 → 绝对时间窗
        day_start = day_end = None
        if day:
            try:
                d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=CST)
            except ValueError:
                return 400, {"error": "invalid_date", "expected": "YYYY-MM-DD"}
            day_start = d0.timestamp()
            day_end = (d0 + timedelta(days=1)).timestamp()

        # 找出该 site 的 cabinet_no 列表
        cabs = srv.tracker.cabinets_for_sites()
        if site_id != "all":
            cabs = [c for c in cabs if srv.site_of(c.get("location")) == site_id]
        nos = [c["cabinet_no"] for c in cabs]
        if not nos:
            return 200, {
                "site": site_id, "hours": hours, "day": day,
                "count": 0, "points": [], "cabinets": 0,
                "window": {"start": iso(day_start), "end": iso(day_end)} if day else None,
                "note": "该区域暂无历史采样。"
            }

        # fault 近似：从 cabinet_state 取当前值
        with srv.tracker._connect() as conn:
            ph = ",".join("?" * len(nos))
            frows = conn.execute(
                f"SELECT cabinet_no, fault FROM cabinet_state WHERE cabinet_no IN ({ph})", nos
            ).fetchall()
        fault_by = {r["cabinet_no"]: int(r["fault"] or 0) for r in frows}

        if day_start is not None:
            raw = srv.tracker.site_samples(nos, hours, start=day_start, end=day_end)
        else:
            raw = srv.tracker.site_samples(nos, hours)

        # 按 10 分钟桶聚合：每柜每桶只取**最后一条**样本（约每 60s 采一次，
        # 一个 10 分钟桶里同一柜会有 ~10 条，直接累加会把数字放大 10 倍）。
        bucket_sec = 600
        # bucket -> cabinet_no -> 该桶内该柜最后一条样本
        per_bucket: dict[int, dict[str, dict]] = {}
        for p in raw:
            b = int(p["at"] // bucket_sec) * bucket_sec
            per_bucket.setdefault(b, {})[p["cabinet_no"]] = p

        points = []
        for b in sorted(per_bucket):
            free = occ = online_n = 0
            for cab_no, p in per_bucket[b].items():
                if not p["online"]:
                    continue
                online_n += 1
                cap = int(p["capacity"])
                chg = int(p["chargeable"])
                flt = min(fault_by.get(cab_no, 0), cap)
                free += chg
                occ += max(0, cap - chg - flt)
            if online_n == 0:
                continue
            points.append({
                "t": iso(b),
                "free": free,
                "occupied": occ,
                "cabinets": online_n,
            })
        return 200, {
            "site": site_id,
            "hours": hours,
            "day": day,
            "count": len(points),
            "cabinets": len(nos),
            "window": {"start": iso(day_start), "end": iso(day_end)} if day else None,
            "points": points,
        }

    def history_days(self, site_id: str, days: int = 7) -> tuple[int, Any]:
        """最近 N 个自然日里，哪些天有采样（给前端日期栏用）。

        只统计该点位电柜的样本行数，不聚合数值——便宜且够用。
        每天返回 `{day, samples, is_today, complete}`，`complete=false` 表示当天还没走完。
        """
        srv = self.srv
        cabs = srv.tracker.cabinets_for_sites()
        if site_id != "all":
            cabs = [c for c in cabs if srv.site_of(c.get("location")) == site_id]
        nos = [c["cabinet_no"] for c in cabs]

        now = datetime.now(CST)
        today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        out = []
        counts: dict[str, int] = {}
        if nos:
            oldest = (today0 - timedelta(days=days - 1)).timestamp()
            ph = ",".join("?" * len(nos))
            with srv.tracker._connect() as conn:
                rows = conn.execute(
                    "SELECT observed_at FROM cabinet_sample "
                    f"WHERE cabinet_no IN ({ph}) AND observed_at >= ?",
                    [*nos, oldest],
                ).fetchall()
            for r in rows:
                d = datetime.fromtimestamp(float(r["observed_at"]), CST).strftime("%Y-%m-%d")
                counts[d] = counts.get(d, 0) + 1
        for i in range(days):
            d0 = today0 - timedelta(days=i)
            key = d0.strftime("%Y-%m-%d")
            is_today = (i == 0)
            out.append({
                "day": key,
                "label": ("今天" if is_today else
                          ("昨天" if i == 1 else f"{d0.month}/{d0.day}")),
                "samples": counts.get(key, 0),
                "is_today": is_today,
                "complete": (not is_today) or (now.hour >= 23),
            })
        return 200, {"site": site_id, "days": out}

