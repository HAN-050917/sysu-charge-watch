# SYSU Charge Watch — API 契约（前后端唯一约定）

版本：v1
后端：Python 3 标准库（无第三方依赖），监听 `127.0.0.1:8787`
数据源：青春号 TOSUN 平台 `https://cloud.tosunelectric.com/v1/user/getCabinetListAround`（免登录，form 编码）

---

## 0. 术语与事实基础

| 概念 | 来源 | 说明 |
|------|------|------|
| 电柜 cabinet | `getCabinetListAround` 一条记录 | `cabinetNo` 如 `CSTFL10643` |
| 桩位 port | `capacity` 字段 | 可能是 `"10"` 或 `"14,6"`（混合柜：柜位+桩位），总数 = 逗号分段求和 |
| 空闲桩位 | `chargeable` 字段 | 服务端直接给的"可充电数" |
| 总数 | `capacity` 求和 | |
| 故障/异常桩位 | `errorCell` 字段 | **推断语义**：平台上报的异常格数。实测恒满足 `chargeable + errorCell <= capacity`，且无一条超过 capacity，因此按"故障/异常桩位数"使用。前端与文档必须标注为推断。|
| 点位 site | `location` 字段 | 中山大学深圳校区共 7 个 location，实际归并为 6 个点位（"西门右侧"与" 西门右侧"合并） |

**关键限制（决定架构）**：`getCabinetListAround` **只返回每台电柜的汇总数**（`chargeable` / `capacity`），
不返回逐桩位的占用状态与已充时长。逐桩位明细接口（`checkCabinetInfo` / `getCaboxListByCabinetNo` /
`queryAllBox` / `queryBoxInfo`）全部要求登录 token。

因此本项目的时长信息由 **后端快照追踪器** 推导（见 §3），并在 UI 上明确标注为"推算"。

---

## 1. GET /api/state

主接口。返回全部点位、电柜、汇总统计与推算结果。

```jsonc
{
  "server_time": "2026-11-19T17:20:00+08:00",
  "server_epoch_ms": 1763563200000,
  "mode": "public",                  // "public" | "auth"（auth 需要用户配置 token）
  "freshness": {
    "interval_seconds": 60,          // 采集间隔
    "last_poll_at": "2026-11-19T17:19:05+08:00",
    "last_poll_ok": true,
    "next_poll_in_seconds": 42,
    "stale": false                   // 超过 150s 未成功采集
  },
  "stats": {
    "cabinets": 47,                  // 在线电柜数（离线不计入）
    "cabinets_all": 48,              // 全部电柜数
    "cabinets_offline": 1,
    "ports_total": 604,              // 仅统计在线电柜的桩位
    "ports_free": 100,               // 平台口径的"空闲"（含断电未取走，会虚高）
    "ports_free_real": 97,           // ★ 真正可用的空位 = ports_free - ports_blocked，UI 应显示这个
    "ports_occupied": 433,
    "ports_fault": 71,
    "ports_blocked": 3,              // ★ cs=6 到点断电但电池未取走，平台算空闲、实际插不进去
    "ports_precise": 12,             // ★ 剩余时长已确定为精确值（目标时长已知）的桩位数
    "free_ratio": 0.1606,            // 按 ports_free_real 计算
    "sites": 6
  },
  "sites": [
    {
      "site_id": "dongjiaoxuelou",
      "name": "东教学楼",
      "raw_locations": ["中山大学(深圳校区)东教学楼"],
      "center": { "lng": 113.95077, "lat": 22.80656 },
      "bounds": { "minLng": 113.95053, "maxLng": 113.95101, "minLat": 22.80617, "maxLat": 22.80698 },
      "stats": {
        "cabinets": 12, "cabinets_online": 12, "cabinets_offline": 0,
        "ports_total": 180, "ports_free": 34, "ports_free_real": 32,
        "ports_occupied": 92, "ports_fault": 54,
        "ports_blocked": 2, "ports_precise": 5,
        "online": 12,
        "free_ratio": 0.1778, "occupancy_ratio": 0.5111
      },
      "level": "mid",                // "none" | "low" | "mid" | "high" | "offline"  → 见 §4
      "offline": false,              // 该点位所有电柜都离线时为 true
      "cabinets": [ /* 见 §2 Cabinet */ ],
      "sample_scope": "campus",      // "campus" = 样本不足回退全校区池；否则为本点位 site_id
      // 点位级推算：当前占用桩位预计多久开始陆续空出
      "forecast": {
        "median_charge_minutes": 180,     // 该点位历史观测中位充电时长（可能为 null）
        "samples": 0,                     // 观测样本数
        "confidence": "none",             // "none" | "low" | "medium" | "high"
        "tracked_occupancies": 3,         // 参与预测的占用数（仅 basis=observed）
        // 未来 15/30/60 分钟内预计释放的桩位数；无样本时为 null（不是 0）
        "expected_free_15m": 0, "expected_free_30m": 0, "expected_free_60m": 0,
        // 预计最早一批空出；无样本时为 null
        "next_free_in_minutes": 175.8
      }
    }
  ]
}
```

## 2. Cabinet 对象

```jsonc
{
  "cabinet_no": "CSTFL10643",
  "site_id": "ximenyouse",
  "location": "中山大学(深圳校区)西门右侧",
  "cabinet_type": 3,
  "type_label": "充电桩",              // 1 换电柜 / 2 充电柜 / 3 充电桩 / 4 汽车充电桩 / 5 混合柜
  "capacity": 10,                      // capacity 逗号求和后的总数
  "capacity_raw": "10",                // 原始字符串，混合柜形如 "14,6"
  "capacity_split": [10],              // 分段：混合柜 [14,6] = 前 14 个柜位 + 后 6 个桩位
  "chargeable": 0,                     // 空闲数
  "occupied": 10,                      // = capacity - chargeable - fault
  "fault": 0,                          // 来自 errorCell（见 §0）
  "online": true,
  "cabinet_status": 1,
  "free_ratio": 0.0,
  "level": "none",                     // "none" | "low" | "mid" | "high" | "offline"
  "lng": 113.941673, "lat": 22.800038,
  "observed_at": "2026-11-19T17:19:05+08:00",
  "changed_at": "2026-11-19T16:52:00+08:00",   // 上一次 chargeable 变化时间
  // 逐桩位占用推算（public 模式下 estimated=true）
  "occupancy": {
    "estimated": true,
    "ports": [
      { "port": 1, "state": "occupied",
        "elapsed_minutes": 42.0, "elapsed_is_lower_bound": false,
        "remaining_minutes": 145.0, "remaining_is_upper_bound": false,
        "median_minutes": 180.0, "basis": "observed", "estimate_basis": "conditional_median" },
      { "port": 2, "state": "free", "elapsed_minutes": null, "elapsed_is_lower_bound": false,
        "remaining_minutes": 0.0, "remaining_is_upper_bound": false,
        "median_minutes": 180.0, "basis": "observed" },
      { "port": 3, "state": "fault", "elapsed_minutes": null, "elapsed_is_lower_bound": false,
        "remaining_minutes": null, "remaining_is_upper_bound": false,
        "median_minutes": null, "basis": "derived" }
    ],
    "note": "桩位号按 chargeable 变化顺序推定，非真实物理编号"
  }
}
```

> `occupancy.ports` 在 public 模式下是**聚合推算**：桩位号是逻辑编号，用来表达"有 N 个占用中、其中 M 个预计 X 分钟内空出"。
> 只有配置了 token + mobile（auth 模式）时才是真实物理 port，真实 `startTime`（`elapsed_minutes` 优先取桩端固件 `ct`）。

### 2.1 前端按现场位置编号（`geoOrderCabs`，纯前端）

Cabinet 对象自带 `lng` / `lat`（WGS84 原始坐标）。前端 `web/app.js` 用这些坐标给同一 `site_id` 的电柜做**现场位置编号**，
解决"光看 CSTFL10643 这种编号不知道是这一排的第几个"的问题；后端不返回序号，完全由前端从同一份坐标算出，与地图天然一致：

- **主轴排序**：把本点位所有柜的经纬度按米制投影（`kx = 111320 × cos(lat)`、`ky = 111320`），
  用 PCA 求这排桩走向 `θ = ½·atan2(2·sxy, sxx−syy)`，把坐标投影到主轴上排序；统一从南端（或西端）定为「第 1 组」，
  保证方向稳定不随昼夜翻转。
- **方向标注**：主轴方向换算成八方位中文（由南向北 / 由西南向东北 / 由西北向东南 …），显示在「分组与桩位」标题栏右侧。
- **现场位置示意条**（`.posstrip`）：按主轴顺序排一行小格，格内序号 + 空闲色点，点格子切换组，
  与组标签的「第 N 组」徽章呼应。这样"从这一排一端数第几个有空位"一眼可见，与地图上的斜排视觉顺序一致。

> 实测（真实坐标）：西门右侧 7 组从西南向东北为 `CSTFL10643 → 10620 → 10633 → 10619 → 10615 → 10616 → CSTFX100051`；
> 东教学楼 12 组由南向北；医学园 5 组由西北向东南。已用 mock 与真实后端两种方式逐点验收。

**充电时长字段（v2，auth 模式）**

`ports[]` 在 auth 模式下额外提供「用户设置的目标时长」链路，字段如下：

| 字段 | 类型 | 含义 |
|------|------|------|
| `elapsed_minutes` | number | 已充分钟数。**优先取桩端固件 `ct`**（实测与 `startTime` 中位偏差 2.5min），其次 `endTime − startTime` |
| `elapsed_is_lower_bound` | bool | `true` 表示已充时长只是下界（public 模式的 `backlog` 占用，或固件未采到时的保守估计） |
| `target_minutes` | number\|null | 用户选择的目标时长（分钟），取值 ∈ `{30,60,120,180,240,300,360,420,480}`；`null` 表示本轮未采到固件 |
| `target_probability` | number\|null | 目标时长成立的概率（0–1）。`1.0` 表示已确定 |
| `target_candidates` | array\|null | 候选档位及概率，按概率降序，最多 4 项 |
| `remaining_minutes` | number\|null | 剩余分钟数 = `target − elapsed` |
| `remaining_max_minutes` | number\|null | 剩余上界。目标未确定时为 `480 − elapsed`（**无条件成立的硬上界**：平台最长充电 480 分钟） |
| `remaining_is_exact` | bool | `true` 表示剩余是**精确值**（非估计） |
| `remaining_is_upper_bound` | bool | `true` 表示 `remaining_minutes` 只是上界，实际可能更早结束 |
| `end_at` | string\|null | 预计结束时刻（ISO 8601，CST）= `startTime + target_minutes` |
| `physically_blocked` | bool | 平台判空闲但电池未取走（固件 `cs=6`），**物理上不可用** |
| `firmware_status` | number\|null | 桩端固件状态码（见下表） |
| `firmware_status_text` | string\|null | 固件状态文案 |
| `energy_kwh` | number\|null | 已充电量（固件 `eg`） |

**目标时长如何得到（三种来源，`estimate_basis` 标注）**

| `estimate_basis` | 条件 | 剩余是否为精确值 |
|---|---|---|
| `firmware_frozen` | 固件 `cs=6`（到点断电）：`ct` 冻结在用户选的目标值上，实测 12/12 精确命中档位 | **是** |
| `slot_constraint` | `ct ≥ 420`：更小的档位早该断电，目标必为 480 | **是** |
| `prior` | `cs=3` 充电中：目标必然 ≥ 下一档位，在学到的档位分布下取最大概率 | 否（给出上界） |
| `firmware_ct` | 只表示已充时长来源，目标未知 | 否 |
| `conditional_median` / `exponential_prior` | 未采到固件，退回历史统计 | 否 |

**固件状态码（`firmware_status`，桩端 `cs` 字段）**

| 码 | 含义 | 说明 |
|---|---|---|
| 0 | 已断电 | 正常空闲 |
| 1 / 2 | 预充 | |
| 3 | 充电中 | 有电流（实测 220V / 0.8A） |
| 5 | 充电停止 | 用户手动结束，`ct` 为任意值，**不可反推目标** |
| 6 | 到点断电 | 电池未取走，`ct` 冻结在目标值上；**平台误判为"空闲"** |
| 8 | 故障 | |
| 10 | 离线 | |

> 平台把 `cs=6`（到点断电、电池未取走）当作**空闲**展示，实际桩位仍被占用。后端单独标记为 `state="blocked"` / `physically_blocked=true`，前端用虚线边框区分，避免用户白跑一趟。
> 实测 2026-11-19 全量扫描：624 端口里 `cs=6` 有 12 个，全部被平台显示为空闲。

**地图字段（前端地图视图消费，后端无需额外工作）**

每台 cabinet 的 `lng` / `lat` 是平台返回的 **WGS84** 坐标。前端地图（第 02 节，Leaflet + 三套可切换底图）：
1. **坐标口径由底图决定（`datum` 自适应）**：`wgs84` 底图直接用原始坐标；`gcj02` 底图先用与小程序 `app-service.js` 模块 `7c94.transformFromWGSToGCJ` 完全一致的算法转成 GCJ02 再落点。切换底图时视野中心会经 `gcj02ToWgs84` 反向解算后重新投影，保持视野不跳。
   > **口径结论（实测）**：用平台**原始坐标**直接落 OSM（WGS84 基准）与 Esri 卫星影像，西园4栋 8 柜精确落在「西园6宿舍/中国农业银行/西园2号楼」真实楼群与路边停车带上；两张独立 WGS84 底图互相印证 → **平台坐标就是 WGS84**。
2. **气泡图标直接用平台真实图片**（`web/vendor/markers/` 本地化 127 张，与小程序 `getIcon` 同源）：`chargeable==0` → 灰泡 `none0`；`1..20` → 按 `cabinet_type` 选前缀（1 换电 `gz` 蓝 / 2 充电柜 `cdg` 绿 / 3 与 5 充电桩与混合柜 `cdz` 黄 / 4 汽车桩 `qcz` 青）+ 空位数；离线 → 红泡 `outLine0`。数字、白边、阴影由图标自带；
3. **marker 本体永远钉在真实坐标，缩放/平移由 Leaflet 重新投影，不漂移**：图标 `iconAnchor` 固定为中心 `[20,21]`，不做任何人为像素散开。平台 48 柜坐标两两不同（实测 exact-same 重合 0 组），因此坐标相近的几组会像小程序那样**沿路自然叠放成斜排**，而不是被推成虚假竖列；
4. 底图按 `街道图(OSM/WGS84) → 高德图(GCJ02) → 卫星图(Esri/WGS84)` 轮换。**高德底图在本片区自带约 150–200m 系统性偏移**（同一坐标在 OSM/卫星图落在宿舍楼群、在高德图却落到校外商铺），因此切到高德时地图注记处会显示橙色告警，明确其不可用于定位；
5. 点击气泡跳到「点位明细」对应组（`site_id` + `cabinet_no`），复用 `UI_STATE` 选中态；
6. 叠放次序：离线柜与空位多的柜排底层，在线且空位多的排上层，避免灰泡盖住彩泡（对齐小程序 `zIndex` 处理）。
地图完全在前端完成，`/api/state` 契约不变。已验收：48 marker 全部渲染 / 0 破损图标 / 五变体零错误、
icon anchor 跨缩放恒 40px、6 个点位的街道图与卫星影像落位逐一核验通过。

**字段语义（重要，UI 必须如实呈现）**

| 字段 | 值 | 含义 |
|------|-----|------|
| `basis` | `observed` | 后端**亲眼看见**由空闲转占用，`elapsed` 准确 |
| | `backlog` | 后端启动时该桩位已在充电，起点未知，`elapsed` 只是**下界** |
| | `unknown` | 有占用数但队列无记录（正常采集后不会出现） |
| `elapsed_is_lower_bound` | `true` | 文案必须是「**至少**已充 42 分钟」 |
| `remaining_is_upper_bound` | `true` | 文案必须是「**至多**还剩 145 分钟」 |
| `remaining_is_exact` | `true` | 文案**不得**加「约 / 至多 / 推断」——这是精确值 |
| `estimate_basis` | `firmware_frozen` | 固件 `cs=6`：`ct` 冻结在用户选的目标上，**精确** |
| | `slot_constraint` | `ct ≥ 420`，只剩 480 一档，**精确** |
| | `prior` | 按学到的档位分布推断，给出概率与上界 |
| | `firmware_ct` | 已充时长来自固件 `ct`，目标未知 |
| | `conditional_median` | 有 ≥8 条真实历史样本，条件生存估计，较可信 |
| | `exponential_prior` | 样本不足，按 180 分钟先验做指数衰减，仅作参考 |

**离线电柜**：`occupancy` 返回 `{"estimated":true, "offline":true, "ports":[], "note":"..."}`。
此时 `level="offline"`，且该柜**不计入**任何 `stats` 汇总（离线残值会虚报空位）。

**★ `ports_free` 与 `ports_free_real` 的区别（UI 必须用后者）**

平台把 `cs=6`（到点断电、电池未取走）的桩位算进 `chargeable`，于是 `ports_free` 会**虚高**。
`ports_free_real = ports_free - ports_blocked` 才是真正能插进去的空位数。
`free_ratio` 与 `level` 都按 `ports_free_real` 计算 —— 否则会出现"显示有空位、跑过去发现插满"的情况。

实测（2026-11-20 02:21）：`ports_free=203`、`ports_blocked=3`、`ports_free_real=200`。
`ports_blocked` 随订单周期波动（实测在 0~22 之间变化，深夜到点断电集中时最多）。

## 3. 推算原理（后端 tracker）

每 60 秒采集一次所有电柜的 `chargeable`，写入 SQLite：

```
poll(ts) → [(cabinet_no, chargeable), ...]
对每台电柜比较 chargeable 的差值：
  Δ = new - old
  Δ < 0  →  新增 |Δ| 个占用事件，start_at = now（进入占用队列）
  Δ > 0  →  从占用队列头部（FIFO）取出 Δ 个，记录一条完整充电记录 duration = now - start_at
```

由完整记录得到 **充电时长分布**（按 cabinet 与按 site 两级；站点级优先，样本更多）：
- `median_charge_minutes`：中位数（带先验收缩，见下）
- `p25/p75`：四分位，用于区间展示

**两个必须说明的建模细节**

1. **backlog 补齐**。后端首次启动时，已经处于占用状态的桩位数量远大于"亲眼看见转占用"的数量
   （实测首轮 434 个）。若不处理，这些桩位永远没有 `elapsed`，UI 会显示一片空白。
   因此每轮采集后按 `occupied = capacity - chargeable - fault` 校验队列长度，缺的补 `basis="backlog"`。
   backlog 的 `started_at` 只是"首次看见"的时间，所以只能作下界；其释放**不计入**时长分布，
   否则会把"未知起点"当成真实时长污染中位数。

2. **先验收缩与条件化剩余**。
   - 中位数收缩：`shrunk = (raw_median * n + 180 * 20) / (n + 20)`，
     避免冷启动头几条"插上就拔"的噪声样本把模型带偏。
   - 剩余时间用条件生存估计 `E[总时长 - 已用 | 总时长 > 已用]`：
     取历史样本中所有 `> elapsed` 的观测求中位数；不足 8 条时退回
     `remaining = PRIOR * exp(-elapsed / PRIOR)`。
     朴素的 `median - elapsed` 在已用超过中位数后会给 0，会错误宣称"马上空出"。

对仍在占用队列中的事件：`elapsed = now - start_at`，再按上述条件化算法求 `remaining`；
按 `remaining` 升序排序后即可给出 "预计 15/30/60 分钟内空出多少个"。

### 3.1 固件链路（auth 模式，优先级高于上述统计）

有 token + mobile 时，后端每轮额外调用 `POST /v1/user/queryBoxInfo`（必带 `port`）读**桩端固件实时状态**，
字段并集 `am/as/bap/bl/bp/cp/cs/ct/dr/eg/fs/hs/im/ir/ls/po/ver/vo`。其中两个字段决定整个时长模型：

- **`ct` = 已充电分钟数**。实测与 `now − startTime` 的偏差中位数仅 2.5 分钟（-0.5 ~ 5.9），且两次采样间隔 3.3 分钟时 `ct` 增量 3~4 —— 确认它随时间递增，是真计时器。
- **`cs` = 桩端状态码**（0 已断电 / 1,2 预充 / 3 充电中 / 5 充电停止 / 6 到点断电 / 8 故障 / 10 离线）。

**用户选的目标时长怎么拿到**：平台不把别人的 `chargeTime` 回吐（`cabox.chargeTime` 对 136 个占用端口恒为 `null`/`0`，
`/v1/user/charge_order_detail/get_charge_order_detail` 所有参数组合都返回 `参数缺失`，固件 `im` 恒 `000000000000000`）。
但桩本身按目标时长计时并到点断电，所以：

- `cs=6`（到点断电）时 **`ct` 冻结在目标值上** → 直接读出用户选的目标时长。实测 12/12 精确命中档位（480×11 + 240×1）。
- `cs=5`（手动停止）时 `ct` 是任意值，**不可反推**（实测 0/9 命中档位）。
- `cs=3`（充电中）时目标必然 ≥ 下一档位；`ct ≥ 420` 时只剩 480 一种可能。

于是 **剩余时长 = 目标时长 − 已充时长**，**结束时刻 = `startTime` + 目标时长**。
目标确定时这是精确值；不确定时给「至多 `480 − ct`」的硬上界（平台最长充电 480 分钟，实测 364 个充电中端口 `ct` 最大 476，无一超 480）。

**采样成本与轮转**：`queryBoxInfo` 串行 6176ms/个、并发 12 时 535ms/个，全量 400 端口需 214s，太重。
因此 `box_sampler.py` 每轮采 180 个（实测 0.691s/端口，约 2 分钟），带轮转游标防止后面的柜长期饿死。
两级 TTL：`CACHE_TTL=110` 秒决定**是否要重采**（与明细刷新对齐），`READ_TTL=900` 秒决定**能否用于展示**——
固件 `ct` 是慢变量、`cs=6` 的冻结值永不变，用 110 秒当展示门槛会把绝大多数已采到的端口判成没数据。
**异常端口优先**：`chargerStatus ∈ {5,6,8,10}` 的端口排在采样队列最前面，第一轮就能发现 `blocked` 端口。
采样范围**必须包含「平台判空闲但 `chargerStatus ∉ {0,1,7}」的端口**，否则永远发现不了 `blocked`。

**★ 端口四分类口径（必须与平台 `chargeable` 恒等）**

平台的 `chargeable` **不是**「可充电数」，实测 48/48 精确吻合的公式是：

```
chargeable == count(infraed == 0 AND status == 1)      # 红外没检测到东西 且 硬件正常
errorCell  == count(status != 1)
```

即 `chargeable` = 「端口空着且硬件正常」的端口数。电池还插着的端口（充满未取、到点断电未取）
**不在** `chargeable` 里 —— 平台没有把它们当空闲，只是不告诉你原因。

后端据此用 `tosun_client.classify_port()` 分四类（判定顺序不可调换）：

| `state` | 判定 | 含义 |
|---|---|---|
| `fault` | `status != 1` | 硬件异常 |
| `free` | `infraed == 0` | 真的空着（**恒等于平台 `chargeable`**） |
| `occupied` | `chargerStatus ∈ {1,2,3}` | 别人正在充 |
| `blocked` | 其余（`infraed==1` 但未充电） | 电池还插在柜里，插不进去 |

**不变量（改口径必须重跑校验）**：
- `free + occupied + blocked + fault == capacity`（48 台全成立）
- 明细里 `state=='free'` 的端口数 `== chargeable`（48 台全成立）

⚠️ **`ports_free` 与 `ports_free_real` 是同一个数**。历史版本曾让 `ports_free_real = max(0, ports_free - ports_blocked)`，
那是错的：`blocked` 端口本来就不在 `chargeable` 里，再减一次会让明细比地图少算 60 个左右。
两个字段现在恒等，保留双名只为兼容旧前端。

**`blocked` 端口带 `physically_blocked: true`**，且其 `voltage`/`ampere`/`energy_kwh` 恒为 0
（到点断电后桩已断输出）。前端应把它渲染成「已断电」而非「充电中」。

**置信度**：`samples >= 30` → high；`>= 8` → medium；`>= 1` → low；`0` → none（此时用全校园先验 180 分钟）。
注意 `confidence="none"` 时 `median_minutes` 仍是 180（先验值），而 `observed_median_minutes` 为 `null` ——
UI 应据 `confidence` 判断可信度，不能因为看到数字就当成真实观测。

**冷启动**：首次运行无历史数据时，后端返回 `confidence: "none"` 且 `forecast` 里各字段为 `null`，
UI 必须显示"数据积累中，约需 1–2 天形成可靠估计"，不得编造数字。

## 4. 等级与配色（前后端共用）

| level | 条件 | 语义 | 色 |
|-------|------|------|-----|
| `none` | free == 0（**且在线**） | 满 | 红 |
| `low` | 0 < free_ratio <= 0.15 | 紧张 | 橙 |
| `mid` | 0.15 < free_ratio <= 0.40 | 一般 | 黄 |
| `high` | free_ratio > 0.40 | 充裕 | 绿 |
| `offline` | 电柜/点位离线 | 状态未知 | 灰 |

`offline` 与 `none` **必须区分**：`none` 表示"柜子在线且确实一个空位都没有"，`offline` 表示
"这台柜子失联，我们什么都不知道"。把离线显示成"满位"会让人白跑一趟。

参考项目用的是"红/黄/绿三档 + 25% 阈值"，本项目改为**四档 + 离线态**，因为中大深圳校区点位空闲率实测跨度大（0% ~ 90%）。

## 5. 其他接口

- `GET /api/site/{site_id}` → 单点位完整数据（结构同 §1 的 sites[i]）
- `GET /api/cabinet/{cabinet_no}` → 单电柜详情（含 `getCabinetInfoByCabinetNo` 的档案：agentName/belongUser/cabImei/amper/hw/fw/rssi/cabTemp/activeDate/expiryDate 等）
- `GET /api/history/{cabinet_no}?hours=24` →
  ```jsonc
  { "cabinet_no": "...", "points": [ { "t": "2026-11-19T17:00:00+08:00", "chargeable": 3, "capacity": 10 } ] }
  ```
- `GET /api/site-history?site={site_id|all}&day={YYYY-MM-DD}` → 区域级**自然日**时序
  （前端第 06 块趋势图的数据源，横轴固定 0:00–23:59）
  ```jsonc
  {
    "site": "all",            // site_id 或 "all"
    "hours": 24,              // 兼容旧参数；给了 day 时忽略
    "day": "2026-11-20",      // 自然日；省略则退化为「最近 hours 小时」滚动窗口
    "count": 125,             // 有效点数
    "cabinets": 48,           // 参与聚合的电柜数
    "window": {               // 自然日窗口（给了 day 时才有），东八区边界
      "start": "2026-11-20T00:00:00+08:00",
      "end":   "2026-11-21T00:00:00+08:00"
    },
    "points": [
      { "t": "2026-11-20T00:00:00+08:00", "free": 137, "occupied": 400, "cabinets": 47 }
    ]
  }
  ```
  - **给了 `day` 就是绝对时间窗 `[当天 00:00, 次日 00:00)`**：X 轴恒为 0:00–23:59，
    当天只采到 20:50 时曲线只画到 20:50 就停，右侧留白——不会被拉伸满屏；
  - 不给 `day` 时退化为旧的滚动窗口（最近 `hours` 小时，边界随当前时刻移动）；
  - `day` 格式非法返回 `400 {"error":"invalid_date","expected":"YYYY-MM-DD"}`；
  - **10 分钟一个桶**（600s）；**每个电柜在每个桶里只取最后一条样本**再求和，避免 60s 级
    原始样本（48 柜 × 1440 轮 ≈ 8.6 万行）把曲线糊成噪声，也避免采样时刻不齐导致虚高。
  - `free` = 在线柜 `chargeable` 之和；`occupied` = `capacity − chargeable − min(fault, capacity)` 之和；
    `cabinets` = 该时刻在线的电柜数（某柜离线时它的桩位从分母里消失，曲线会出现真实台阶）。
  - `fault` 取 `cabinet_state` 的当前值近似（不是历史逐点值），这是已知的近似，不影响可用/占用两条主曲线。
  - `site` 传具体点位时按 `site_of(location)` 过滤电柜；离线点位返回 `count: 0`。
- `GET /api/history-days?site={site_id|all}&days=7` → 最近 N 个自然日**哪些天有采样**
  （前端趋势图日期栏的数据源，`days` clamp 1..31）
  ```jsonc
  {
    "site": "all",
    "days": [
      { "day": "2026-11-20", "label": "今天", "samples": 68496, "is_today": true,  "complete": false },
      { "day": "2026-11-19", "label": "昨天", "samples": 18384, "is_today": false, "complete": true  },
      { "day": "2026-11-18", "label": "11/18", "samples": 0,    "is_today": false, "complete": true  }
    ]
  }
  ```
  - `samples` = 该天该点位电柜的原始采样**行数**（只数不聚合，很便宜）；`0` 表示这天没跑过服务；
  - `complete=false` 仅出现在今天（这一天还没走完），前端据此显示「进行中」；
  - 数组按**从今天往过去**倒序，前端直接按顺序渲染成按钮。
- `GET /api/health` → `{"ok":true,"poll_age_seconds":37,"db_rows":1234}`
- `POST /api/poll` → 立即触发一次采集（限频 20 秒），返回 `{"ok":true}` 或 `{"ok":false,"reason":"rate_limited"}`
- `GET /api/config` / `POST /api/config` → 读取/写入 `token`（auth 模式）与 `poll_interval`、`scan_distance_km`；`POST {"token":"..."}` 保存即验证，响应附 `auth_check`
- `GET /api/auth` → 当前鉴权与真实数据状态（同 `/api/state` 的 `auth` 对象）
- `POST /api/auth/check` → `{"force":true}` 强制重新验证 token；有效时顺带刷新逐桩位真实数据，返回最新 `auth_status()`
- `POST /api/auth/details` → `{"force":true}` 强制刷新逐桩位真实数据（120 秒内自动限频）
- `GET /api/meta` → 静态元数据：点位列表（含中文名、坐标、电柜清单）、类型标签表、图例、免责声明

### 5.1 鉴权与逐桩位真实数据（v1.1 新增）

配置 token 后，`/api/state` 顶层 `auth` 对象：

```jsonc
{
  "mode": "public" | "auth",
  "token_configured": true,
  "token_preview": "********0000",   // 只回后四位，完整 token 绝不出现在任何响应里
  "valid": true,                      // checkTokenValid 结果
  "reason": null,                     // 失效时为「会话过期,请重新登录」等
  "mobile_masked": "138****0000",
  "detail_active": true,              // 逐桩位真实数据已接入
  "detail_error": null,
  "detail_cabinets": 48,              // 成功拉取到的电柜数
  "real_ports": 624,                  // 拉到的真实桩位总数（含空闲/占用/故障）
  "details_at": "2026-11-19T18:30:00+08:00",
  "refreshing": false                 // 后台是否正在拉取逐桩位真实数据
}
```

- token **和 mobile 都为空** → `mode=public`，所有桩位仍是推算值；凭证失效或接口报错 → **优雅降级**回推算，不会让页面崩溃。
- 有真实数据的桩位 `occupancy.source = "platform_real"`，`occupancy.ports[].source = "platform_real"`，cabinet 级 `detail_source = "platform_real"`。
- 真实桩位的 `elapsed_minutes = endTime − startTime`（**真值**，`elapsed_is_lower_bound=false`）；**平台的 `chargeTime` 字段实测恒为 `null`，不可用作时长**。占用判定为 `chargeOrderNo` 以 `CD` 开头且 `chargerStatus ∈ {1,2,3}`（排除历史残留订单）。附加字段 `cabox_id`/`charger_status_text`/`battery_type`/`percentage`/`infraed`/`voltage`/`ampere`/`start_time`/`end_time`。
- **平台没有"剩余时长"字段**，所以真实桩位的 `remaining_minutes` 仍由历史模型条件估计，`estimate_basis` 如实标注为 `conditional_median` 或 `exponential_prior`。
- 逐桩位真实数据由**后台线程**刷新（120 秒限频，实测 48 柜约 76 秒），**不阻塞 `/api/state`**（首屏秒开，真实数据到位后下一次轮询自动切换）；token 30 秒内复用验证结果，避免高频打平台接口。
- `getCaboxListByCabinetNo` 必须同时带 `token` 与 `mobile` 两个参数，缺 `mobile` 会返回「登录状态已经失效」。
- token / mobile 只保存在本机 `data/config.json`，仅用于只读状态查询；前端不直连平台、不写 localStorage、不把凭证拼进 URL。

## 6. 静态资源

`/` → `web/index.html`；`/app.js`、`/styles.css`、`/assets/*` 直接映射到 `web/` 目录。

## 7. 免责声明（必须在 UI 首页可见）

> 本页面数据来自青春号 TOSUN 公开的附近电柜查询接口（`getCabinetListAround`），
> 仅做只读聚合与展示，不涉及登录、账户、支付、订单或启停充电。
> "已充时长 / 剩余时长"为基于空闲数变化的历史推算，**非平台官方数据**，仅供参考。
