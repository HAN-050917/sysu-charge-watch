"""用户充电目标时长的学习与反推。

## 要解决的问题
用户明确要求：用「用户设置的充电时长 + 开始充电时间」算出
  ① 已充时长   ② 剩余时长   ③ 结束/转空闲时刻

## 平台的能力边界（全部实测）
  * `startTime`     精确可用（平台逐桩位接口返回）
  * `ct`            精确可用（固件 queryBoxInfo，已充分钟数，与 startTime 差 <3min）
  * 目标时长           **对别人的订单不回吐**：
                      - cabox.chargeTime 恒为 null（136 个占用端口全 None/0）
                      - /v1/user/charge_order_detail/get_charge_order_detail
                        所有参数组合都返回 {"code":1,"msg":"参数缺失"}
                      - 固件 im 字段恒 "000000000000000"，与时长无关
  * 但有一个突破口：`cs=6`（到点断电、电池未取走）时 `ct` **冻结在目标值上**，
    实测 12/12 精确命中 timeList 档位（480×11 + 240×1）。

## 因此本模块的做法
1. **学习**：每次采样发现 cs=6，就把冻结的 ct 归档到目标档位，
   持续积累「本校用户实际选择的目标时长分布」。
2. **反推**：对充电中的桩位，已知 ct，则目标 T 必然 ≥ next_slot(ct)
   （否则早就断电了），在这个约束下用学到的分布取最大概率档位。
3. **产出**：`target` → 结束时刻 = startTime + target，
   剩余 = target - ct。给出置信度，让前端能区分「确定」与「推断」。

## 为什么这比纯统计好
旧模型用「已结束会话的时长中位数」猜剩余，受右删失偏差影响（观测窗口
只有几小时，长会话根本不会结束、进不了样本），且不利用「目标只能取
离散档位」这一强约束。新模型直接对齐平台真实的断电机制。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import tosun_client

# 观测到的目标档位计数 {(分钟): 次数}，持久化到 tracker 的 kv
KV_KEY = "target_slot_counts"

# 已经计数过的 cs=6 观测指纹，用于去重。
# cs=6（到点断电未取走）状态会持续几小时，每轮采样都会再次读到同一个
# 冻结 ct；若不去重，同一笔订单会被重复计数几十次，把分布彻底带偏。
SEEN_KEY = "target_seen_cs6"

# 去重表上限，超出后丢弃最旧的（这些端口早已被取走，不会再复现）
_SEEN_LIMIT = 4000

# 目标档位的先验计数。来自 2026-11-19 首次全量扫描：12 个 cs=6 端口的
# 冻结 ct 精确命中档位，分布为 480×11 + 240×1。
# 这不是猜测 —— 是实测到的本校用户真实选择偏好，作为冷启动先验很合理：
# 绝大多数人选「充满」（8 小时），少数人选 4 小时。
_PRIOR_COUNTS = {480: 11, 240: 1}

# 加性平滑系数（Laplace）。12 个样本覆盖不到 9 个档位中的 7 个，
# 若直接按 0 概率处理就等于宣称"没人会选 1 小时"，这是过度自信。
# 每个档位补 alpha 个伪计数，保证任何档位都保留非零概率。
_SMOOTHING = 1.0

_LOCK = threading.Lock()


def _load() -> dict[str, int]:
    import tracker

    raw = tracker.get_kv(KV_KEY)
    if isinstance(raw, dict):
        out = {}
        for k, v in raw.items():
            try:
                out[str(int(k))] = int(v)
            except (TypeError, ValueError):
                continue
        return out
    return {}


def _save(counts: dict[str, int]) -> None:
    import tracker

    tracker.set_kv(KV_KEY, counts)


def observe(boxes: list[dict[str, Any]]) -> int:
    """从一批固件读数里学习目标时长（只认 cs=6 的冻结 ct）。

    去重口径：同一「柜号+端口+冻结 ct」只计一次。
    cs=6 状态会持续数小时，每轮采样都会读到同一个冻结值；不做去重的话
    一笔订单会被计几十次，把分布带偏。
    """
    if not boxes:
        return 0
    added = 0
    with _LOCK:
        counts = _load()
        seen = _load_seen()
        for box in boxes:
            if not isinstance(box, dict):
                continue
            if box.get("cs") != tosun_client.CS_REACHED_TARGET:
                continue
            target = tosun_client.infer_target_minutes(box)
            if target is None:
                continue
            fp = "%s:%s:%d" % (
                str(box.get("cabinet_no") or ""),
                str(box.get("port") or ""),
                int(target),
            )
            if fp in seen:
                continue
            seen[fp] = int(time.time())
            key = str(int(target))
            counts[key] = counts.get(key, 0) + 1
            added += 1
        if added:
            _save(counts)
            _save_seen(seen)
    return added


def _load_seen() -> dict[str, int]:
    import tracker

    raw = tracker.get_kv(SEEN_KEY)
    if isinstance(raw, dict):
        out = {}
        for k, v in raw.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out
    return {}


def _save_seen(seen: dict[str, int]) -> None:
    import tracker

    if len(seen) > _SEEN_LIMIT:
        # 按时间戳保留最新的部分
        keep = sorted(seen.items(), key=lambda kv: -kv[1])[: _SEEN_LIMIT // 2]
        seen = dict(keep)
    tracker.set_kv(SEEN_KEY, seen)


def distribution() -> dict[str, Any]:
    """返回当前学到的目标时长分布（实测观测 + 先验 + 平滑，带概率）。"""
    counts = _load()
    slots = list(tosun_client.TARGET_MINUTES)
    # 每个档位 = 先验计数 + 实际观测计数 + 平滑伪计数
    combined = {
        s: _PRIOR_COUNTS.get(s, 0) + counts.get(str(s), 0) + _SMOOTHING for s in slots
    }
    total = sum(combined.values()) or 1.0
    probs = {s: combined[s] / total for s in slots}
    observed_total = sum(counts.values())
    top = sorted(
        ({"minutes": s, "count": counts.get(str(s), 0), "prob": round(probs[s], 4)}
         for s in slots),
        key=lambda r: -r["prob"],
    )
    return {
        "slots": slots,
        "counts": {str(s): counts.get(str(s), 0) for s in slots},
        "prior_counts": {str(k): v for k, v in _PRIOR_COUNTS.items()},
        "probabilities": {str(s): round(probs[s], 4) for s in slots},
        "observed_samples": observed_total,
        "prior_samples": sum(_PRIOR_COUNTS.values()),
        "smoothing": _SMOOTHING,
        "top": top[:3],
        "source": "observed_cs6+prior" if observed_total else "prior",
    }


def predict_target(ct: float, cs: int | None = None) -> dict[str, Any] | None:
    """由已充分钟数反推最可能的目标时长。

    约束：目标 T 必然 ≥ next_slot(ct)（更小的档位早该断电了）。
    在该约束下取学习分布的最大概率档位。

    返回 {target_minutes, probability, candidates, exact, source}
      exact=True 表示 ct≥420，目标只可能是 480，无需猜测。
    """
    if ct is None:
        return None
    try:
        ct = float(ct)
    except (TypeError, ValueError):
        return None
    if ct < 0:
        return None

    # cs=6 走确定性路径
    if cs == tosun_client.CS_REACHED_TARGET:
        target = tosun_client.infer_target_minutes({"cs": cs, "ct": ct})
        if target is not None:
            return {
                "target_minutes": target,
                "probability": 1.0,
                "candidates": [{"minutes": target, "prob": 1.0}],
                "exact": True,
                "source": "firmware_frozen",
            }

    slots = list(tosun_client.TARGET_MINUTES)
    # 硬约束：目标不可能小于下一个档位
    nxt = next((t for t in slots if t >= ct), int(tosun_client.MAX_CHARGE_MINUTES))
    allowed = [t for t in slots if t >= nxt]
    if not allowed:
        return None

    if len(allowed) == 1:
        # 只剩 480 一种可能 → 精确
        return {
            "target_minutes": allowed[0],
            "probability": 1.0,
            "candidates": [{"minutes": allowed[0], "prob": 1.0}],
            "exact": True,
            "source": "slot_constraint",
        }

    dist = distribution()
    probs = dist["probabilities"]
    weights = [(t, float(probs.get(str(t), 0.0))) for t in allowed]
    total = sum(w for _, w in weights) or 1.0
    norm = [(t, w / total) for t, w in weights]
    norm.sort(key=lambda kv: -kv[1])
    best_t, best_p = norm[0]
    return {
        "target_minutes": int(best_t),
        "probability": round(best_p, 4),
        "candidates": [{"minutes": int(t), "prob": round(p, 4)} for t, p in norm[:4]],
        "exact": False,
        "source": "learned_distribution" if dist["observed_samples"] else "prior",
    }


def plan(box: dict[str, Any], start_time_epoch: float | None = None) -> dict[str, Any] | None:
    """把「目标时长 + 已充时长」换算成结束时刻与剩余时长。

    返回：
      target_minutes       目标时长（分钟）
      target_exact         目标是否确定
      target_probability   目标为真的概率（exact 时为 1.0）
      elapsed_minutes      已充分钟数（固件 ct，精确）
      remaining_minutes    剩余分钟数 = target - ct
      end_at               预计结束时刻（ISO 字符串，CST）
      end_at_epoch         预计结束时刻（Unix 秒）
      source               依据
    """
    if not box or box.get("ct") is None:
        return None
    ct = float(box["ct"])
    cs = box.get("cs")
    pred = predict_target(ct, cs)
    if pred is None:
        return None

    target = pred["target_minutes"]
    remaining = max(0.0, float(target) - ct)
    end_epoch = None
    end_iso = None
    if start_time_epoch:
        # 结束时刻 = 开始充电时刻 + 目标时长
        end_epoch = float(start_time_epoch) + target * 60.0
        from datetime import datetime, timedelta, timezone

        cst = timezone(timedelta(hours=8))
        end_iso = datetime.fromtimestamp(end_epoch, cst).isoformat(timespec="seconds")

    return {
        "target_minutes": int(target),
        "target_exact": pred["exact"],
        "target_probability": pred["probability"],
        "target_candidates": pred["candidates"],
        "elapsed_minutes": round(ct, 1),
        "remaining_minutes": round(remaining, 1),
        "end_at": end_iso,
        "end_at_epoch": int(end_epoch) if end_epoch else None,
        "source": pred["source"],
    }


def stats() -> dict[str, Any]:
    return distribution()
