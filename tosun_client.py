"""青春号 TOSUN 平台只读客户端。

仅调用免登录的公开查询接口：
  - POST /v1/user/getCabinetListAround      附近电柜（含 chargeable / capacity 汇总）
  - POST /v1/user/getCabinetInfoByCabinetNo 单台电柜档案
  - POST /v1/user/getChargePerHour          充电单价

不涉及登录、账户、支付、订单、启停充电。

平台实现要点（逆向自小程序 wx98d7c7d09b88b452）：
  * 必须使用 application/x-www-form-urlencoded 提交，JSON body 会被拒。
  * 该接口不校验 signature/token，但仍按平台约定带上空值头以保持兼容。
  * 单次查询服务端硬上限 100 条，无分页参数 → 用多中心点扫描后按 cabinetNo 去重。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Iterable

BASE = "https://cloud.tosunelectric.com"
SOURCE = "CSTF"
TIMEOUT = 20
# 单次并发扫描的中心点数量上限（避免串行累加超时）
CENTER_CONCURRENCY = 6

EP_LIST_AROUND = "/v1/user/getCabinetListAround"
EP_CABINET_INFO = "/v1/user/getCabinetInfoByCabinetNo"
EP_CHARGE_PER_HOUR = "/v1/user/getChargePerHour"
# 以下端点需要真实登录 token（由用户在看板「登录凭证」面板手动提供）
EP_CHECK_TOKEN = "/v1/user/checkTokenValid"
EP_USER_INFO = "/v1/user/getUserInfo"
EP_CHECK_CABINET = "/v1/user/checkCabinetInfo"          # 只给 6 字段简表，无时长
EP_CABOX_LIST = "/v1/user/getCaboxListByCabinetNo"     # 完整逐桩位：含 chargeOrderNo/startTime/endTime/chargerStatus/voltage/ampere

CABINET_TYPES = {
    1: "换电柜",
    2: "充电柜",
    3: "充电桩",
    4: "汽车充电桩",
    5: "混合柜",
}


def capacity_split(raw: Any) -> list[int]:
    """'10' -> [10]；'14,6' -> [14, 6]（混合柜：柜位 + 桩位）。"""
    if raw is None:
        return []
    out: list[int] = []
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = int(float(chunk))
        except ValueError:
            continue
        if value > 0:
            out.append(value)
    return out


def capacity_total(raw: Any) -> int:
    return sum(capacity_split(raw))


def _nonce() -> str:
    """等价于小程序的 UUID v4 nonce 生成。"""
    return str(uuid.uuid4())


def signature(params: dict[str, Any], timestamp: int, nonce: str) -> str:
    """复刻平台签名：排序后的 k=v 用 & 连接，再拼 timestamp/nonceStr，取 MD5 小写。

    逆向自小程序 app-service.js 的 request 封装：
        A = "&".join(f"{k}={v}" for k in sorted(params))
        A += f"&timestamp={ts}&nonceStr={nonce}"
        header.signature = md5(A)
    公开查询接口实际不校验签名，但仍按平台约定计算，便于日后切换账号态接口。
    """
    pairs = [f"{k}={'' if v is None else v}" for k, v in sorted(params.items())]
    plain = "&".join(pairs)
    plain += f"&timestamp={timestamp}&nonceStr={nonce}"
    return hashlib.md5(plain.encode("utf-8")).hexdigest()


def _post(
    endpoint: str,
    params: dict[str, Any],
    token: str = "",
    timeout: float | None = None,
) -> dict[str, Any]:
    clean = {k: ("" if v is None else v) for k, v in params.items()}
    body = urllib.parse.urlencode(clean).encode("utf-8")
    timestamp = int(time.time())
    nonce = _nonce()
    request = urllib.request.Request(
        BASE + endpoint,
        data=body,
        method="POST",
        headers={
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "timestamp": str(timestamp),
            "nonceStr": nonce,
            "token": token,
            "signature": signature(clean, timestamp, nonce),
            "accept": "application/json",
        },
    )
    timeout = TIMEOUT if timeout is None else timeout
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", "replace")
    return json.loads(text)


def _check(payload: dict[str, Any], endpoint: str) -> Any:
    code = payload.get("code")
    if code != 0:
        raise RuntimeError(f"{endpoint} 返回错误: {payload.get('msg')} (code={code})")
    return payload.get("data")


def list_around(
    centers: Iterable[tuple[float, float]],
    distance_km: int,
    token: str = "",
    timeout: float | None = None,
) -> dict[str, dict[str, Any]]:
    """多中心点扫描附近电柜，按 cabinetNo 去重合并。

    单次调用服务端上限 100 条且无分页，因此必须用多个中心点覆盖整个校园。
    中心点**并发**请求：串行时 6 次 × 20s 超时会拖死整轮采集。
    返回 {cabinetNo: 原始记录}。

    `distance` 有两个实测约束（都容易踩）：
      1. 单位是**公里**。distance=1 时最远返回 0.723 km，distance=5 时最远 4.900 km，
         结果按距离升序。早期形参写成 `distance_m` 传 2500 是误当米用，碰巧
         2500 km 已覆盖整个珠三角才没出错，属于"靠容错跑对"。
      2. 必须是**整数**。服务端是 Spring 的 Integer 绑定，传 "3.0" 会抛
         `NumberFormatException: For input string: "3.0"`（code=500）。
    """
    points = list(centers)
    merged: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    distance = int(distance_km)

    def _one(point: tuple[float, float]) -> tuple[tuple[float, float], Any, str | None]:
        lng, lat = point
        try:
            data = _check(
                _post(
                    EP_LIST_AROUND,
                    {
                        "longitude": lng,
                        "latitude": lat,
                        "distance": distance,
                        "batteryTypes": "",
                        "mobile": "",
                        "source": SOURCE,
                    },
                    token=token,
                    timeout=timeout,
                ),
                EP_LIST_AROUND,
            )
            return point, data, None
        except (urllib.error.URLError, OSError, RuntimeError, ValueError) as exc:
            return point, None, str(exc)

    with ThreadPoolExecutor(max_workers=min(CENTER_CONCURRENCY, max(1, len(points)))) as pool:
        for point, data, error in pool.map(_one, points):
            if error is not None:
                errors.append(f"{point[0]},{point[1]}: {error}")
                continue
            for item in data or []:
                cabinet_no = item.get("cabinetNo")
                if cabinet_no:
                    # 保留距离最近的一条（distance 是字符串浮点）
                    previous = merged.get(cabinet_no)
                    if previous is None or _distance(item) < _distance(previous):
                        merged[cabinet_no] = item

    if not merged and errors:
        raise RuntimeError("所有中心点均采集失败: " + "; ".join(errors[:3]))
    return merged


def _distance(item: dict[str, Any]) -> float:
    try:
        return float(item.get("distance") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def cabinet_profile(cabinet_no: str) -> dict[str, Any]:
    """单台电柜档案。失败返回空 dict（档案是增强信息，不应阻塞主流程）。"""
    try:
        data = _check(_post(EP_CABINET_INFO, {"cabinetNo": cabinet_no}), EP_CABINET_INFO)
        return data if isinstance(data, dict) else {}
    except (urllib.error.URLError, OSError, RuntimeError, ValueError):
        return {}


def charge_per_hour() -> float | None:
    try:
        value = _check(_post(EP_CHARGE_PER_HOUR, {}), EP_CHARGE_PER_HOUR)
        return float(value)
    except (urllib.error.URLError, OSError, RuntimeError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------- auth-required endpoints

# 平台在会话失效时返回的典型文案，用于把"失败"和"接口异常"区分开
_AUTH_ERROR_HINTS = ("登录状态已经失效", "会话过期", "请重新登录", "未登录", "token")


def is_auth_error(payload: dict[str, Any]) -> bool:
    """判断平台响应是否为鉴权失败（而不是网络/业务错误）。"""
    if not isinstance(payload, dict) or payload.get("code") == 0:
        return False
    msg = str(payload.get("msg") or "")
    return any(hint in msg for hint in _AUTH_ERROR_HINTS)


def check_token(token: str, timeout: float | None = None) -> dict[str, Any]:
    """验证 token 是否有效。

    返回 {"valid": bool, "reason": str, "user": dict|None}。
    平台没有干净的"校验通过"接口：checkTokenValid 在 token 有效时通常 code=0，
    失效时 code=1 + "会话过期"。失败的 HTTP/解析异常统一当成 invalid，不抛给上层。
    """
    if not token:
        return {"valid": False, "reason": "未配置 token", "user": None}
    try:
        payload = _post(EP_CHECK_TOKEN, {}, token=token, timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
        return {"valid": False, "reason": f"校验请求失败: {exc}", "user": None}
    if payload.get("code") == 0:
        data = payload.get("data")
        user = data if isinstance(data, dict) else None
        return {"valid": True, "reason": "ok", "user": user}
    return {"valid": False, "reason": str(payload.get("msg") or "会话过期"), "user": None}


def cabinet_detail(cabinet_no: str, token: str, mobile: str = "", timeout: float | None = None) -> dict[str, Any]:
    """逐桩位真实状态（需登录 token + 手机号）。

    实测（2026-11-19）：/v1/user/checkCabinetInfo 在 mobile 为空时返回
    "登录状态已经失效"；mobile 必传。且它只给 6 个字段（port/infraed/
    percentage/status/batteryType/batteryColor），没有时长。真正完整的逐桩位
    数据在 /v1/user/getCaboxListByCabinetNo（含 chargeOrderNo/startTime/
    endTime/chargerStatus/voltage/ampere），本函数改用后者。

    返回 {"ok": bool, "caboxs": list, "reason": str, "raw": dict}。
    """
    if not token:
        return {"ok": False, "caboxs": [], "reason": "未配置 token", "raw": {}}
    if not mobile:
        return {"ok": False, "caboxs": [], "reason": "缺少手机号（无法查询逐桩位）", "raw": {}}
    try:
        payload = _post(
            EP_CABOX_LIST,
            {"cabinetNo": cabinet_no, "mobile": mobile},
            token=token,
            timeout=timeout,
        )
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
        return {"ok": False, "caboxs": [], "reason": f"请求失败: {exc}", "raw": {}}
    if payload.get("code") != 0:
        return {
            "ok": False,
            "caboxs": [],
            "reason": str(payload.get("msg") or "未知错误"),
            "raw": payload,
        }
    data = payload.get("data")
    caboxs = data if isinstance(data, list) else []
    return {
        "ok": True,
        "caboxs": caboxs,
        "reason": "ok",
        "raw": {"count": len(caboxs)},
    }


def cabinet_details(
    cabinet_nos: list[str],
    token: str,
    mobile: str = "",
    timeout: float | None = None,
    max_workers: int = 16,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """并发拉取多台电柜的逐桩位真实状态（需登录 token）。

    返回 (details, errors)：
      details = {cabinetNo: [cabox, ...]}  仅含成功且 code=0 的柜
      errors  = {cabinetNo: reason}        失败的柜（含鉴权失效）
    调用方据此决定整体降级还是部分降级。
    """
    details: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    if not token or not cabinet_nos:
        return details, errors

    def _one(no: str) -> tuple[str, dict[str, Any]]:
        return no, cabinet_detail(no, token, mobile, timeout)

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(cabinet_nos)))) as pool:
        for no, result in pool.map(_one, cabinet_nos):
            if result.get("ok"):
                details[no] = result["caboxs"]
            else:
                errors[no] = str(result.get("reason") or "未知错误")
    return details, errors


def cabox_charger_status_text(value: Any) -> str:
    """复刻小程序的 chargerStatus 文案表。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    return {0: "待机", 7: "待机", 1: "充电中", 3: "充电中", 2: "预充", 5: "充电停止", 6: "未充满"}.get(n, "")


def is_cabox_occupied(cabox: dict[str, Any]) -> bool:
    """实测修正的占用判定。

    早期文档说"chargeOrderNo 以 CD 开头即占用"，实测发现不成立：
    有些桩位 chargeOrderNo=CD... 但 chargerStatus=0（待机）、endTime 是昨天，
    属于历史残留订单。真正"正在充电"的判据必须是 chargerStatus ∈ {1,2,3}
    且 endTime ≥ startTime。这里只认活跃充电状态，避免把残留订单算成占用。
    """
    order_no = str(cabox.get("chargeOrderNo") or "")
    if not order_no.startswith("CD"):
        return False
    try:
        status = int(cabox.get("chargerStatus"))
    except (TypeError, ValueError):
        return False
    return status in (1, 2, 3)  # 充电中/预充/充电中


def charge_elapsed_minutes(cabox: dict[str, Any], now: datetime | None = None) -> float | None:
    """从 startTime/endTime 算真实已充分钟数。

    实测 chargeTime 字段恒为 null，不可用。充电中桩位的 endTime 会被平台
    实时刷新到当前时刻（例：startTime 12:39:29, endTime 19:10:53 → 已充 6h31m）。
    所以 已充时长 = endTime - startTime。若 endTime 缺失或早于 startTime，返回 None。
    """
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        start = datetime.strptime(str(cabox.get("startTime")), fmt)
        end = datetime.strptime(str(cabox.get("endTime")), fmt)
    except (TypeError, ValueError):
        return None
    if end < start:
        return None
    return round((end - start).total_seconds() / 60.0, 1)


def type_label(cabinet_type: Any) -> str:
    try:
        return CABINET_TYPES.get(int(cabinet_type), "充电柜")
    except (TypeError, ValueError):
        return "充电柜"


# ---------------------------------------------------------------- 固件实时状态
#
# 2026-11-19 实测确认（本次逆向的关键突破）：
#   queryBoxInfo 必须带 port，返回 data.box[0] 为固件实时读数：
#     cs = 固件充电状态   3=正在充电(220V/0.8A)  0/5=已断电  6=到点断电(未充满)
#     ct = 已充电分钟数   cs=3 时随时间递增；断电后**冻结**在断电那一刻
#     vo = 电压(V)  am = 电流(A)  eg = 电量(kWh)  ir = 红外
#
#   决定性证据（对照实验）：
#     cs=6「未充满」12 个端口，ct 冻结值 = 480×11 + 240×1 → 12/12 精确命中
#       小程序 timeList 的目标时长（480min=8h「充满」、240min=4h）
#     cs=5「充电停止」9 个端口，ct = 87/1/141/272/476/11/33/218/165 → 0/9 命中
#   ⇒ 桩确实按用户选择的目标时长计时并到点断电，cs=6 冻结的 ct 就是该目标时长。
#   ⇒ 已充时长从此可用 ct 精确读取，不再依赖 endTime-startTime 或历史统计。

# 小程序充电页 timeList: ["充满", .5, 1, 2, 3, 4, 5, 6, 7, 8]（小时），"充满"提交为 8
TARGET_HOURS = (0.5, 1, 2, 3, 4, 5, 6, 7, 8)
TARGET_MINUTES = tuple(int(h * 60) for h in TARGET_HOURS)

# 平台硬上限：最长 480 分钟（8 小时）。三条独立证据：
#   1. cs=6 的 12 个端口 ct 冻结值 = 480×11 + 240×1（12/12 命中档位）
#   2. cs=3 的 364 个充电中端口 ct 最大 476，无一超过 480
#   3. 小程序 timeList 最大档 8 小时，「充满」按 8*chargePerHour 计费
# ⇒ 正在充电的端口一定在 ct=480 前断电，故「剩余 ≤ 480-ct」是**精确上界**。
# ⇒ ct>420 时目标只可能是 480（420 档早在 420 分钟就断电了）→ 剩余为精确值。
MAX_CHARGE_MINUTES = 480.0
# ct 超过这个值，说明更小的档位都已排除，目标必然是 480。
TARGET_CERTAIN_MINUTES = 420.0

# 固件 cs 码表（与平台 chargerStatus 基本一一对应，但 cs 是桩端实时值）
CS_TEXT = {
    0: "已断电",
    1: "预充",
    2: "预充",
    3: "充电中",
    5: "充电停止",
    6: "到点断电",
    8: "故障",
    10: "离线",
}

# cs 语义分组
CS_CHARGING = 3          # 正在充电（有电流）
CS_REACHED_TARGET = 6    # 到达用户设定的目标时长后断电（ct 冻结在目标值）
CS_STOPPED = 5           # 用户手动结束 / 异常停止
CS_IDLE = 0              # 已断电（含陈旧订单）


def box_info(
    cabinet_no: str,
    port: int,
    token: str,
    mobile: str = "",
    timeout: float | None = None,
) -> dict[str, Any]:
    """查询单个端口的固件实时状态。失败返回 {}，不抛异常。

    注意：queryBoxInfo 不带 port 会返回空 data，必须逐端口查。
    """
    if not token or not str(mobile or "").strip():
        return {}
    try:
        payload = _post(
            "/v1/user/queryBoxInfo",
            {"cabinetNo": cabinet_no, "mobile": mobile, "port": int(port)},
            token=token,
            timeout=timeout or 12,
        )
    except Exception:
        return {}
    if payload.get("code") != 0:
        return {}
    data = payload.get("data") or {}
    boxes = data.get("box") or []
    if not boxes or not isinstance(boxes[0], dict):
        return {}
    parsed = parse_box(boxes[0])
    # 回填身份字段：采样去重、前端定位都要靠它们
    parsed["cabinet_no"] = cabinet_no
    parsed["port"] = int(port)
    return parsed


def parse_box(box: dict[str, Any]) -> dict[str, Any]:
    """把固件 box 字段（大小写双份）规整成小写键 + 数值。"""
    if not isinstance(box, dict):
        return {}
    low = {str(k).lower(): v for k, v in box.items()}

    def num(key):
        v = low.get(key)
        if v in (None, ""):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    cs = num("cs")
    ct = num("ct")
    return {
        "cs": int(cs) if cs is not None else None,
        "cs_text": CS_TEXT.get(int(cs), "") if cs is not None else "",
        "ct": ct,                       # 已充电分钟数（断电后冻结）
        "voltage": num("vo"),
        "ampere": num("am"),
        "energy_kwh": num("eg"),
        "infraed": low.get("ir"),
        "port": low.get("po"),
    }


def is_box_charging(box: dict[str, Any]) -> bool:
    """固件是否真的在充电（有电流）。cs=3 且电流>0.05A 才算。"""
    if not box:
        return False
    if box.get("cs") != CS_CHARGING:
        return False
    amp = box.get("ampere")
    return amp is not None and amp > 0.05


def infer_target_minutes(box: dict[str, Any]) -> int | None:
    """从固件状态推断用户选择的目标时长（分钟）。

    只在能**确定**时返回：
      * cs=6（到点断电）：ct 冻结在目标值上，取最近档位即为目标
        （实测 12/12 精确命中：480×11 + 240×1）
      * cs=3（充电中）且 ct > 420：更小的档位早已断电，目标必然是 480
        （实测 364 个充电中端口 ct 最大 476，无一超 480）
    其余情况目标未知，返回 None。
    """
    if not box:
        return None
    ct = box.get("ct")
    if ct is None or ct <= 0:
        return None
    cs = box.get("cs")
    if cs == CS_REACHED_TARGET:
        # 冻结值可能有 1-2 分钟抖动，取最近的合法档位
        best = min(TARGET_MINUTES, key=lambda t: abs(t - ct))
        if abs(best - ct) <= 5:
            return int(best)
        return None
    if cs == CS_CHARGING and ct >= TARGET_CERTAIN_MINUTES:
        # ct 已超过除 480 外所有档位，目标只可能是 480
        return int(MAX_CHARGE_MINUTES)
    return None


def remaining_bounds(box: dict[str, Any]) -> dict[str, Any] | None:
    """计算「还剩多少时间转空闲」。

    平台完全没有倒计时字段，但固件给了 ct（已充分钟数）与 cs（充电状态）。
    结合两条硬约束可以给出**有依据的剩余区间**，而不是拍脑袋猜：

    约束 A：目标时长只能取小程序 timeList 的离散档位
            {30, 60, 120, 180, 240, 300, 360, 420, 480} 分钟。
    约束 B：到点必定断电（实测 cs=6 的 12 个端口 ct 冻结值 12/12 命中档位）。

    于是目标 T 必然满足 T ≥ next_slot(ct)（否则在更早的档位就该断了），
    而 T ≤ 480（平台硬上限，实测 364 个充电中端口 ct 最大 476，无一超 480）。

    剩余时间因此落在：
      min = next_slot(ct) - ct   下界，**前提是用户不手动结束**
                                 （实测 364 个充电中端口仅 14 个 cs=5 手动停止，占 3.7%）
      max = 480 - ct             上界，**无条件成立**
    ct ≥ 420 时 next_slot 与上限都是 480，区间收敛为精确值。

    返回 {remaining_min_minutes, remaining_max_minutes, is_exact,
          target_minutes, min_target_minutes, elapsed_minutes, stopped}
    """
    if not box:
        return None
    ct = box.get("ct")
    if ct is None:
        return None
    ct = float(ct)
    cs = box.get("cs")

    if cs == CS_REACHED_TARGET:
        # 已到点断电：充电已结束，不会再"转空闲"（桩早就断了）
        target = infer_target_minutes(box)
        return {
            "remaining_min_minutes": 0.0,
            "remaining_max_minutes": 0.0,
            "is_exact": True,
            "target_minutes": target,
            "min_target_minutes": target,
            "elapsed_minutes": ct,
            "stopped": True,
        }

    if cs == CS_CHARGING:
        nxt = next((t for t in TARGET_MINUTES if t >= ct), int(MAX_CHARGE_MINUTES))
        lo = round(max(0.0, nxt - ct), 1)
        hi = round(max(0.0, MAX_CHARGE_MINUTES - ct), 1)
        target = infer_target_minutes(box)
        return {
            "remaining_min_minutes": lo,
            "remaining_max_minutes": hi,
            "is_exact": target is not None,
            "target_minutes": target,
            "min_target_minutes": int(nxt),
            "elapsed_minutes": ct,
            "stopped": False,
        }

    # cs=0/5/8 等已断电状态：已经断了，不会自动转空闲
    target = infer_target_minutes(box)
    return {
        "remaining_min_minutes": 0.0,
        "remaining_max_minutes": 0.0,
        "is_exact": True,
        "target_minutes": target,
        "min_target_minutes": target,
        "elapsed_minutes": ct,
        "stopped": True,
    }


def physically_blocked(box: dict[str, Any]) -> bool:
    """桩位是否「平台说空闲、但物理上还占着」。

    实测发现的重要陷阱：cs=6（到点断电、电池未取走）时，平台
    `chargerStatus=6`、`is_cabox_occupied()` 返回 False，即**平台把该桩位
    当空闲展示**。但用户的电池还在柜里，别人插不进去。
    本项目要把这层区分出来，否则用户看到"空闲"跑过去会白跑。
    """
    if not box:
        return False
    return box.get("cs") == CS_REACHED_TARGET


# ---------------------------------------------------------------- 桩位分类（统一口径）
#
# 🔴 2026-11-20 实测校正：平台的 `chargeable` 语义**不是**"没在充电的端口数"，
#    而是 **`infraed == 0 且 status == 1` 的端口数**（48/48 柜精确吻合，三轮复测稳定）。
#
#    `infraed`（红外）表示"端口里是否插着电池/充电器"。平台只把**没插东西**的
#    端口算作可充电。因此以下两类端口平台都不计入 `chargeable`：
#      * infraed==1 且正在充电（chargerStatus ∈ {1,2,3}）→ 占用
#      * infraed==1 但已断电（chargerStatus ∈ {0,5,6,8,10}）→ 电池仍插着，插不进去
#
#    早期实现只用"没在充电"判 free，把后一类（实测约 60 个端口）算成了空闲，
#    导致点位明细的空闲数比地图/平台 `chargeable` 高出 60 个左右 —— 这就是
#    "点位明细与电柜地图数据不同步"的真正原因（不是刷新延迟）。
#
#    另一个反直觉点：`cs=6` 的端口 `infraed` 恒为 1（实测 29/29），
#    所以平台**本来就没把它们算进 chargeable**，不存在"平台把 cs=6 当空闲"的 bug。
#    之前那条结论是错的，已在文档中更正。

PORT_FREE = "free"
PORT_OCCUPIED = "occupied"
PORT_BLOCKED = "blocked"
PORT_FAULT = "fault"


def _int_of(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def classify_port(cabox: dict[str, Any]) -> str:
    """把单个端口归入四类之一，口径与平台 `chargeable` 完全一致。

    判定顺序（与平台一致，不可调换）：
      1. `status != 1`            → fault    硬件故障/离线，平台既不算空闲也不算占用
      2. `infraed == 0`           → free     端口里没插东西 = 真正可用
      3. 充电中（cs ∈ {1,2,3}）   → occupied 正在充电
      4. 其余（infraed==1 未充电）→ blocked  插着电池但没在充（到点断电/手动停止等）

    保证：free 的计数恒等于平台的 `chargeable`；
          四类之和恒等于该柜的 `capacity`。
    """
    status = cabox.get("status")
    status_int = 1 if status in (None, "") else _int_of(status, 1)
    if status_int != 1:
        return PORT_FAULT
    if _int_of(cabox.get("infraed")) == 0:
        return PORT_FREE
    try:
        charger_status = int(cabox.get("chargerStatus"))
    except (TypeError, ValueError):
        charger_status = None
    if charger_status in (1, 2, 3):
        return PORT_OCCUPIED
    return PORT_BLOCKED


def port_state_of(cabox: dict[str, Any], box: dict[str, Any] | None = None) -> str:
    """在 `classify_port` 基础上用固件读数做一次交叉校验。

    固件 `cs` 比平台 `chargerStatus` 更实时：若平台还显示充电中但固件已报
    cs=6（到点断电），以固件为准归为 blocked —— 否则用户会看到一个"正在充电"
    但实际早已断电的桩位。

    注意：**不改变 free 的判定**。free 必须严格等于平台 `chargeable`，
    否则又会出现明细与地图不一致。
    """
    state = classify_port(cabox)
    if state != PORT_OCCUPIED or not box:
        return state
    if box.get("cs") == CS_REACHED_TARGET:
        return PORT_BLOCKED
    return state


def count_port_states(caboxs: list[dict[str, Any]]) -> dict[str, int]:
    """统计一批端口的四类计数（不含固件交叉校验，纯平台口径）。"""
    out = {PORT_FREE: 0, PORT_OCCUPIED: 0, PORT_BLOCKED: 0, PORT_FAULT: 0}
    for cabox in caboxs or []:
        if isinstance(cabox, dict):
            out[classify_port(cabox)] += 1
    return out
