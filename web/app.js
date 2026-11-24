/* ==========================================================================
   中大深圳 · 充电桩空位 — 前端逻辑
   纯原生 JS（ES2020），无框架、无构建、无外部依赖。
   数据来源：GET /api/state（见 docs/API.md）
   演示模式：?mock 使用内置真实样本快照；另支持 ?cold / ?offline / ?fail 三个自检变体。
   ========================================================================== */
(function () {
  'use strict';

  /* ------------------------------------------------------------------------
     0. 运行参数与常量
     ------------------------------------------------------------------------ */
  const params = new URLSearchParams(location.search);
  const USE_MOCK = params.has('mock');
  const FLAGS = {
    cold: USE_MOCK && params.has('cold'),       // 全部预测置为冷启动 → 验证「数据积累中」
    offline: USE_MOCK && params.has('offline'), // 食堂整点位离线 → 验证离线卡片
    fail: USE_MOCK && params.has('fail'),       // 模拟接口失败 → 验证错误态
    real: USE_MOCK && params.has('real'),       // 模拟逐桩位真实数据 → 验证「实测」标签与附加字段
    full: USE_MOCK && params.has('full')        // 让首个区域满员 → 验证「无空闲 → 最快空出」分支
  };

  const REFRESH_SECONDS = 60;
  const RETRY_SECONDS = 20;
  const FETCH_TIMEOUT_MS = 8000;
  const POLL_SETTLE_MS = 900;

  /* 等级文案：none 专指「在线的柜子真的 0 空位」，offline 指电柜失联、状态未知 */
  const LEVEL_TEXT = { none: '已满', low: '紧张', mid: '一般', high: '充裕', offline: '离线' };
  const CONF_TEXT = { none: '无估计', low: '置信度 低', medium: '置信度 中', high: '置信度 高' };
  const TYPE_LABEL = { 1: '换电柜', 2: '充电柜', 3: '充电桩', 4: '汽车充电桩', 5: '混合柜' };
  /* 估计依据：有真实样本 vs 经验先验 */
  const BASIS_TEXT = { conditional_median: '条件中位数', exponential_prior: '基于经验先验' };
  /* 起点来源：看见的开始（准）/ 观察前就已占用（下界）/ 未知 */
  const ORIGIN_TEXT = { observed: '起点已知', backlog: '观察前已占用', unknown: '起点未知' };
  const SCOPE_TEXT = { campus: '样本来自全校区池' };
  /* 中位充电时长的兜底值：与 tracker.py 的 PRIOR_MINUTES 保持一致 */
  const PRIOR_MINUTES = 180;

  /* 分组查看的选中态：跨 60s 轮询重建 DOM 时保留用户的「组 / 桩位」选择。
     key = site_id，value = { cab: 电柜编号, port: 端口序号 } */
  const UI_STATE = Object.create(null);

  /* ------------------------------------------------------------------------
     1. DOM 引用
     ------------------------------------------------------------------------ */
  const $ = (id) => document.getElementById(id);
  const dom = {
    deckCabinets: $('deck-cabinets'),
    badge: $('mode-badge'),
    badgeText: $('mode-text'),
    factPoll: $('fact-poll'),
    factAge: $('fact-age'),
    countdown: $('fact-countdown'),
    btnRefresh: $('btn-refresh'),
    btnRefreshText: $('btn-refresh-text'),
    track: $('refresh-track'),
    note: $('status-note'),
    authToken: $('auth-token'),
    authMobile: $('auth-mobile'),
    authPaste: $('auth-paste'),
    authParse: $('auth-parse'),
    authPasteHint: $('auth-paste-hint'),
    authSave: $('auth-save'),
    authCheck: $('auth-check'),
    authClear: $('auth-clear'),
    authState: $('auth-state'),
    statRow: $('stat-row'),
    insights: $('insights'),
    compareList: $('compare-list'),
    finderChips: $('finder-chips'),
    finderResult: $('finder-result'),
    finderRoot: $('finder-root'),
    finderToggle: $('finder-toggle'),
    trendAreas: $('trend-areas'),
    trendChart: $('trend-chart'),
    trendFoot: $('trend-foot'),
    trendDaybar: $('trend-daybar'),
    trendDays: $('trend-days'),
    trendDaytoggle: $('trend-daytoggle'),
    trendDaycur: $('trend-daycur'),
    trendMetrics: document.querySelectorAll('.trend__metric'),
    siteGrid: $('site-grid'),
    mapEl: $('cabinet-map'),
    mapReset: $('map-reset'),
    mapBase: $('map-base'),
    mapNote: $('map-note')
  };

  /* ------------------------------------------------------------------------
     2. 小工具
     ------------------------------------------------------------------------ */
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const key in attrs) {
        const val = attrs[key];
        if (val === null || val === undefined || val === false) continue;
        if (key === 'text') node.textContent = String(val);
        else if (key === 'class') node.className = val;
        else if (key === 'style') node.setAttribute('style', val);
        else if (key === 'dataset') { for (const k in val) node.dataset[k] = val[k]; }
        else node.setAttribute(key, val === true ? '' : String(val));
      }
    }
    if (children) {
      for (const child of [].concat(children)) {
        if (child === null || child === undefined || child === false) continue;
        node.append(child);
      }
    }
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function num(v, fallback) {
    const n = typeof v === 'number' ? v : parseFloat(v);
    return Number.isFinite(n) ? n : (fallback === undefined ? 0 : fallback);
  }

  function pct(v) { return Math.max(0, Math.min(100, num(v) * 100)); }

  /* capacity_raw 形如 "14,6" 或 "10"，求和得到总桩位数 */
  function capacitySum(raw) {
    if (typeof raw === 'number') return raw;
    if (typeof raw !== 'string') return 0;
    return raw.split(/[,，\s]+/).reduce((acc, piece) => acc + (parseInt(piece, 10) || 0), 0);
  }

  /* 时长：<60 分 → "83 分"，否则 "1 小时 23 分" */
  function fmtDuration(minutes) {
    if (minutes === null || minutes === undefined || !Number.isFinite(Number(minutes))) return '—';
    const m = Math.round(Number(minutes));
    if (m <= 0) return '不足 1 分';
    if (m < 60) return m + ' 分';
    const h = Math.floor(m / 60);
    const rest = m % 60;
    return rest ? h + ' 小时 ' + rest + ' 分' : h + ' 小时';
  }

  /* 剩余时长：<=0 一律显示「即将空出」 */
  function fmtRemaining(minutes) {
    if (minutes === null || minutes === undefined || !Number.isFinite(Number(minutes))) return '—';
    const m = Math.round(Number(minutes));
    if (m <= 0) return '即将空出';
    return fmtDuration(m);
  }

  function fmtClock(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '—';
    const pad = (n) => String(n).padStart(2, '0');
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  /* 由空闲数与总数推出等级（API §4）
     离线 → "offline"（状态未知），不再混进 "none"（在线的柜子真的 0 空位） */
  function levelOf(free, total, offline) {
    if (offline) return 'offline';
    if (!total || free <= 0) return 'none';
    const ratio = free / total;
    if (ratio <= 0.15) return 'low';
    if (ratio <= 0.40) return 'mid';
    return 'high';
  }

  function levelOfRatio(ratio, free, offline) {
    if (offline) return 'offline';
    if (!(free > 0)) return 'none';
    if (ratio <= 0.15) return 'low';
    if (ratio <= 0.40) return 'mid';
    return 'high';
  }

  /* 电柜是否离线：优先用后端 level，其次 online */
  function isOfflineCab(cab) {
    return cab.level === 'offline' || cab.online === false;
  }

  function fetchJSON(url, options) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    return fetch(url, Object.assign({ signal: controller.signal, headers: { Accept: 'application/json' } }, options))
      .then((res) => {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .finally(() => clearTimeout(timer));
  }

  /* ------------------------------------------------------------------------
     3. 内置 mock 数据（?mock）
     基于 _sysu_live.json 的 48 台真实电柜样本：每站点柜数 / 桩位数 / 空闲 / 故障
     与真实聚合值一致；cabinet_no 中少量为真实值，其余为演示占位编号。
     元组：[cabinet_no, site_id, cabinet_type, capacity_raw, chargeable, fault, online]
     ------------------------------------------------------------------------ */
  const MOCK_SITES = [
    { site_id: 'ximenyouse',     name: '西门右侧',              short: '西门', raw: ['西门右侧', ' 西门右侧'], center: { lng: 113.94186, lat: 22.80031 } },
    { site_id: 'xiyuan4',        name: '西园4栋宿舍',            short: '西4',  raw: ['西园4栋宿舍'],     center: { lng: 113.93904, lat: 22.80171 } },
    { site_id: 'yixueyuan',      name: '医学园',                short: '医学', raw: ['医学园'],          center: { lng: 113.94654, lat: 22.80055 } },
    { site_id: 'dongjiaoxuelou', name: '东教学楼',              short: '东教', raw: ['东教学楼'],       center: { lng: 113.95077, lat: 22.80656 } },
    { site_id: 'dongyuanshitang', name: '东园食堂地下停车场',    short: '食堂', raw: ['东园食堂地下停车场'], center: { lng: 113.95648, lat: 22.80363 } },
    { site_id: 'dongyuansushe',  name: '东园宿舍',              short: '东宿', raw: ['东园宿舍'],       center: { lng: 113.95912, lat: 22.80481 } }
  ];

  /* 48 台真实电柜的经纬度（来自 _sysu_live.json / getCabinetListAround，WGS84）。
     演示模式下用于地图 marker 定位；占位编号不在表内时按站点中心确定性散开。 */
  const MOCK_COORDS = {"CSTFL10643":[113.941673,22.800038],"CSTFL10620":[113.941763,22.800203],"CSTFL10633":[113.941883,22.800318],"CSTFL10619":[113.941949,22.800471],"CSTFL10615":[113.942078,22.80062],"CSTFL10616":[113.942182,22.800727],"CSTFX100051":[113.942422,22.800782],"CSTFN40046":[113.939146,22.801498],"CSTFN40040":[113.939016,22.801622],"CSTFL10094":[113.93919,22.801827],"CSTFL10078":[113.939056,22.801782],"CSTFN40043":[113.938961,22.801744],"CSTFL10150":[113.939156,22.801943],"CSTFL10145":[113.939026,22.801904],"CSTFN40038":[113.938896,22.801862],"CSTFL10107":[113.946266,22.800717],"CSTFL10073":[113.946436,22.800766],"CSTFN40041":[113.946642,22.800758],"CSTFN40034":[113.946745,22.800585],"CSTFN40045":[113.946855,22.800335],"CSTFN40030":[113.950594,22.806166],"CSTFN40042":[113.950594,22.806432],"CSTFN40044":[113.950533,22.80659],"CSTFL10617":[113.950579,22.806776],"CSTFN40047":[113.950719,22.806687],"CSTFN40039":[113.950729,22.806719],"CSTFL10646":[113.950754,22.806796],"CSTFL10614":[113.950639,22.806975],"CSTFN40035":[113.951008,22.806629],"CSTFL10635":[113.950758,22.806952],"CSTFL10074":[113.950952,22.806758],"CSTFL10113":[113.950978,22.806968],"CSTFN40033":[113.956478,22.803629],"CSTFL10100":[113.958309,22.804188],"CSTFL10079":[113.958529,22.804274],"CSTFL10611":[113.95872,22.804334],"CSTFL10642":[113.958884,22.804483],"CSTFL10631":[113.959078,22.804595],"CSTFL10613":[113.959333,22.804682],"CSTFX100036":[113.959452,22.80471],"CSTFL10610":[113.959492,22.804758],"CSTFL10653":[113.959652,22.804907],"CSTFL10648":[113.959802,22.805025],"CSTFL10654":[113.959872,22.805117],"CSTFL10649":[113.960011,22.805242],"CSTFL10625":[113.960121,22.805349],"CSTFL10628":[113.960266,22.805428],"CSTFL10621":[113.960327,22.805525],"CSTFL10651":[113.94144,22.80009],"CSTFL10638":[113.94186,22.80031],"CSTFL10627":[113.94228,22.80053],"CSTFN40051":[113.93841,22.80149],"CSTFL10087":[113.93883,22.80171],"CSTFL10102":[113.93925,22.80193],"CSTFL10111":[113.93967,22.80149],"CSTFL10115":[113.94654,22.80033],"CSTFN40024":[113.94909,22.80634],"CSTFN40019":[113.94951,22.80656],"CSTFN40036":[113.94993,22.80678],"CSTFN40028":[113.95035,22.80634],"CSTFL10082":[113.95077,22.80656],"CSTFL10091":[113.95119,22.80678],"CSTFL10063":[113.95161,22.80634],"CSTFL10058":[113.95203,22.80656],"CSTFL10069":[113.95245,22.80678],"CSTFL10096":[113.95681,22.80459],"CSTFL10109":[113.95723,22.80481],"CSTFL10112":[113.95765,22.80503],"CSTFL10118":[113.95807,22.80459],"CSTFL10123":[113.95849,22.80481],"CSTFL10131":[113.95891,22.80503],"CSTFL10140":[113.95933,22.80459],"CSTFL10148":[113.95975,22.80481],"CSTFL10152":[113.96017,22.80503],"CSTFL10160":[113.96059,22.80459],"CSTFL10167":[113.96101,22.80481],"CSTFL10174":[113.96143,22.80503]};

  const MOCK_CABS = [
    /* 西门右侧：7 台 · 72 桩位 · 空闲 12 · 故障 2 */
    ['CSTFL10643', 'ximenyouse', 3, '10', 0, 1, 1],
    ['CSTFL10620', 'ximenyouse', 3, '10', 1, 1, 1],
    ['CSTFL10651', 'ximenyouse', 3, '10', 1, 0, 1],
    ['CSTFL10638', 'ximenyouse', 3, '10', 0, 0, 1],
    ['CSTFL10627', 'ximenyouse', 3, '10', 0, 0, 1],
    ['CSTFL10614', 'ximenyouse', 3, '10', 0, 0, 1],
    ['CSTFX100051', 'ximenyouse', 1, '12', 10, 0, 1],
    /* 西园4栋宿舍：8 台 · 120 桩位 · 空闲 30 · 故障 4 */
    ['CSTFN40046', 'xiyuan4', 5, '14,6', 9, 0, 1],
    ['CSTFN40038', 'xiyuan4', 5, '14,6', 2, 1, 1],
    ['CSTFN40042', 'xiyuan4', 5, '14,6', 6, 0, 1],
    ['CSTFN40051', 'xiyuan4', 5, '14,6', 3, 1, 1],
    ['CSTFL10094', 'xiyuan4', 3, '10', 1, 2, 1],
    ['CSTFL10087', 'xiyuan4', 3, '10', 3, 0, 1],
    ['CSTFL10102', 'xiyuan4', 3, '10', 4, 0, 1],
    ['CSTFL10111', 'xiyuan4', 3, '10', 2, 0, 1],
    /* 医学园：5 台 · 80 桩位 · 空闲 7 · 故障 1 */
    ['CSTFL10107', 'yixueyuan', 3, '10', 0, 0, 1],
    ['CSTFL10115', 'yixueyuan', 3, '10', 0, 0, 1],
    ['CSTFN40041', 'yixueyuan', 5, '14,6', 4, 0, 1],
    ['CSTFN40034', 'yixueyuan', 5, '14,6', 1, 0, 1],
    ['CSTFN40045', 'yixueyuan', 5, '14,6', 2, 1, 1],
    /* 东教学楼：12 台 · 180 桩位 · 空闲 30 · 故障 54 */
    ['CSTFN40030', 'dongjiaoxuelou', 5, '14,6', 6, 6, 1],
    ['CSTFN40047', 'dongjiaoxuelou', 5, '14,6', 2, 9, 1],
    ['CSTFN40024', 'dongjiaoxuelou', 5, '14,6', 4, 5, 1],
    ['CSTFN40019', 'dongjiaoxuelou', 5, '14,6', 4, 4, 1],
    ['CSTFN40036', 'dongjiaoxuelou', 5, '14,6', 3, 3, 1],
    ['CSTFN40028', 'dongjiaoxuelou', 5, '14,6', 3, 2, 1],
    ['CSTFL10074', 'dongjiaoxuelou', 3, '10', 0, 10, 1],
    ['CSTFL10082', 'dongjiaoxuelou', 3, '10', 2, 6, 1],
    ['CSTFL10091', 'dongjiaoxuelou', 3, '10', 1, 4, 1],
    ['CSTFL10063', 'dongjiaoxuelou', 3, '10', 3, 5, 1],
    ['CSTFL10058', 'dongjiaoxuelou', 3, '10', 0, 0, 1],
    ['CSTFL10069', 'dongjiaoxuelou', 3, '10', 2, 0, 1],
    /* 东园食堂地下停车场：1 台 · 20 桩位 · 空闲 18 · 故障 0（真实样本曾短暂离线） */
    ['CSTFN40033', 'dongyuanshitang', 5, '14,6', 18, 0, 1],
    /* 东园宿舍：15 台 · 152 桩位 · 空闲 15 · 故障 10 */
    ['CSTFX100036', 'dongyuansushe', 1, '12', 10, 0, 1],
    ['CSTFL10100', 'dongyuansushe', 3, '10', 1, 0, 1],
    ['CSTFL10096', 'dongyuansushe', 3, '10', 0, 2, 1],
    ['CSTFL10653', 'dongyuansushe', 3, '10', 1, 0, 1],
    ['CSTFL10109', 'dongyuansushe', 3, '10', 0, 1, 1],
    ['CSTFL10112', 'dongyuansushe', 3, '10', 0, 0, 1],
    ['CSTFL10118', 'dongyuansushe', 3, '10', 1, 3, 1],
    ['CSTFL10123', 'dongyuansushe', 3, '10', 0, 1, 1],
    ['CSTFL10131', 'dongyuansushe', 3, '10', 0, 0, 1],
    ['CSTFL10140', 'dongyuansushe', 3, '10', 1, 0, 1],
    ['CSTFL10148', 'dongyuansushe', 3, '10', 0, 0, 1],
    ['CSTFL10152', 'dongyuansushe', 3, '10', 0, 2, 1],
    ['CSTFL10160', 'dongyuansushe', 3, '10', 0, 1, 1],
    ['CSTFL10167', 'dongyuansushe', 3, '10', 1, 0, 1],
    ['CSTFL10174', 'dongyuansushe', 3, '10', 0, 0, 0]
  ];

  /* 每站点的预测值（模拟稳定运行 1–2 天后的状态） */
  const MOCK_FORECAST = {
    ximenyouse:     { median: 82,  samples: 36, confidence: 'high',   f15: 1, f30: 3, f60: 6,  next: 14 },
    xiyuan4:        { median: 95,  samples: 22, confidence: 'medium', f15: 2, f30: 5, f60: 9,  next: 9 },
    yixueyuan:      { median: 74,  samples: 12, confidence: 'medium', f15: 0, f30: 1, f60: 3,  next: 21 },
    dongjiaoxuelou: { median: 110, samples: 41, confidence: 'high',   f15: 2, f30: 4, f60: 8,  next: 11 },
    dongyuanshitang: { median: 64, samples: 5,  confidence: 'low',    f15: 3, f30: 6, f60: 10, next: 6 },
    dongyuansushe:  { median: 88,  samples: 29, confidence: 'medium', f15: 1, f30: 2, f60: 5,  next: 17 }
  };

  function hashStr(str) {
    let h = 2166136261;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  /* 由电柜汇总数构造「推定桩位」：占用排在前面，故障其次，空闲在后。
     字段对齐契约：basis / estimate_basis / elapsed_is_lower_bound / remaining_is_upper_bound */
  function mockOccupancy(cab, median, nowMs, samples) {
    const ports = [];
    const total = cab.capacity;
    const occupied = Math.min(cab.occupied, total);
    const fault = Math.min(cab.fault, total - occupied);
    const free = Math.max(0, total - occupied - fault);
    const enough = num(samples) >= 8;
    const estimateBasis = enough ? 'conditional_median' : 'exponential_prior';
    const TARGET_SLOTS = [30, 60, 120, 180, 240, 300, 360, 420, 480];
    let counter = 0;
    for (let i = 0; i < occupied; i++) {
      counter++;
      const seed = hashStr(cab.cabinet_no + '#' + i);
      /* 已充时长分布：大部分是短时（6–123 分），每 7 个里有 1 个是长时充电
         （421–478 分）—— 后者用来覆盖「已充 ≥420 分钟 ⇒ 目标必为 480 ⇒ 剩余精确」
         这条确定性分支，否则该分支在演示模式下永远走不到。 */
      const longCharge = (seed % 7) === 0;
      const elapsed = longCharge ? 421 + (seed % 58) : 6 + (seed % 118);
      const remain = Math.max(0, median - elapsed);
      /* 一半桩位模拟「观察前就已占用」（backlog，下界），一半模拟「看见开始」（observed，准确） */
      const seenFromStart = (seed % 3) === 0;
      const port = {
        port: counter,
        state: 'occupied',
        elapsed_minutes: elapsed,
        elapsed_is_lower_bound: !seenFromStart,
        remaining_minutes: remain,
        remaining_max_minutes: remain,
        remaining_is_upper_bound: !seenFromStart,
        remaining_is_exact: false,
        median_minutes: median,
        basis: seenFromStart ? 'observed' : 'backlog',
        estimate_basis: estimateBasis
      };
      /* ?mock&real 下补上「目标时长」链路，用于验证前端精确/推断两种展示 */
      if (FLAGS.real) {
        /* 目标档位：不小于已充时长的最近档位（与后端硬约束一致） */
        const target = TARGET_SLOTS.find((t) => t >= elapsed) || 480;
        const exact = target === 480 && elapsed >= 420;
        const startMs = nowMs - elapsed * 60000;
        port.source = 'platform_real';
        port.cabox_id = cab.cabinet_no + '-' + (i + 1);
        port.charger_status_text = '充电中';
        port.firmware_status_text = '充电中';
        port.battery_type = '锂电池';
        port.percentage = 20 + (seed % 70);
        port.voltage = 220.0;
        port.ampere = 0.5 + (seed % 60) / 100;
        /* 电量 = 电压 × 电流 × 时长（kWh），与物理一致：220V×0.7A×1.4h ≈ 0.22kWh */
        port.energy_kwh = Math.round(220 * port.ampere * (elapsed / 60) / 1000 * 100) / 100;
        port.start_time = new Date(startMs).toISOString();
        port.target_minutes = target;
        port.target_probability = exact ? 1.0 : Math.min(0.95, 0.5 + elapsed / 900);
        port.remaining_is_exact = exact;
        port.remaining_minutes = Math.max(0, target - elapsed);
        port.remaining_max_minutes = Math.max(0, 480 - elapsed);
        port.remaining_is_upper_bound = !exact;
        port.end_at = new Date(startMs + target * 60000).toISOString();
        port.estimate_basis = exact ? 'slot_constraint' : 'prior';
      }
      /* ?mock&real 下再抽 1/9 的桩位模拟 cs=6「到点断电但电池未取走」：
         平台把它算空闲，实际插不进去。这条分支决定 ports_blocked / blocked 状态
         能否在演示模式被看到，否则前端 CSS 与文案永远验证不到。 */
      if (FLAGS.real && (seed % 9) === 4) {
        const frozenTarget = TARGET_SLOTS[seed % TARGET_SLOTS.length];
        port.state = 'blocked';
        port.physically_blocked = true;
        port.elapsed_minutes = frozenTarget;
        /* 固件 ct 是精确计时器，不是「至少」下界 */
        port.elapsed_is_lower_bound = false;
        port.target_minutes = frozenTarget;
        port.target_probability = 1.0;
        port.remaining_minutes = 0;
        port.remaining_max_minutes = 0;
        port.remaining_is_exact = true;
        port.remaining_is_upper_bound = false;
        port.estimate_basis = 'firmware_frozen';
        port.firmware_status_text = '到点断电';
        port.charger_status_text = '到点断电';
        port.start_time = new Date(nowMs - frozenTarget * 60000).toISOString();
        port.end_at = new Date(nowMs).toISOString();
        /* 到点断电后桩已断输出：无电压/电流/电量，与固件实测一致 */
        port.voltage = 0;
        port.ampere = 0;
        port.energy_kwh = 0;
      }
      ports.push(port);
    }
    for (let i = 0; i < fault; i++) {
      counter++;
      ports.push({
        port: counter, state: 'fault',
        elapsed_minutes: null, elapsed_is_lower_bound: false,
        remaining_minutes: null, remaining_is_upper_bound: false,
        median_minutes: median, basis: 'unknown', estimate_basis: estimateBasis
      });
    }
    for (let i = 0; i < free; i++) {
      counter++;
      ports.push({
        port: counter, state: 'free',
        elapsed_minutes: null, elapsed_is_lower_bound: false,
        remaining_minutes: null, remaining_is_upper_bound: false,
        median_minutes: median, basis: 'unknown', estimate_basis: estimateBasis
      });
    }
    return FLAGS.real
      ? { estimated: false, offline: false, source: 'platform_real', ports: ports,
          note: '逐桩位状态与已充时长来自平台真实数据；目标时长已知时剩余为精确值，否则为上界' }
      : { estimated: true, offline: false, ports: ports, note: '桩位号按 chargeable 变化顺序推定，非真实物理编号' };
  }

  /* 离线电柜：不再返回桩位数组，明确「最后一次读数不作为当前桩位状态」 */
  function offlineOccupancy() {
    return { estimated: true, offline: true, ports: [], note: '电柜离线，最后一次读数不作为当前桩位状态' };
  }

  function buildMockState() {
    const nowMs = Date.now();
    const sites = MOCK_SITES.map((meta) => {
      const forc = MOCK_FORECAST[meta.site_id] || {};
      const median = forc.median || PRIOR_MINUTES;
      const isOfflineSite = FLAGS.offline && meta.site_id === 'dongyuanshitang';
      const cabs = MOCK_CABS
        .filter((row) => row[1] === meta.site_id)
        .map((row) => {
          const [cabinetNo, siteId, type, raw, chargeable, fault, online] = row;
          const capacity = capacitySum(raw);
          const onlineValue = isOfflineSite ? 0 : online;
          /* full 演示：所有区域电柜满员（chargeable 归零），用于验证
             finder 的「无空闲 → 最快空出 3–5 个」分支 */
          const effChargeable = FLAGS.full ? 0 : chargeable;
          const occupiedRaw = Math.max(0, capacity - effChargeable - fault);
          const rt = capacity ? chargeable / capacity : 0;
          const occupancy = onlineValue
            ? mockOccupancy({ cabinet_no: cabinetNo, capacity: capacity, occupied: occupiedRaw, fault: fault }, median, nowMs, forc.samples)
            : offlineOccupancy();
          /* 🔴 blocked 必须从桩位列表数出来并回填到电柜级，否则点位卡片的
             「断电未取走 N 个」在演示模式下永远不显示（后端真实数据是电柜级字段）。 */
          const blockedN = (occupancy.ports || []).filter((p) => p.state === 'blocked').length;
          const occupied = Math.max(0, occupiedRaw - blockedN);
          const cab = {
            cabinet_no: cabinetNo,
            site_id: siteId,
            location: meta.name,
            cabinet_type: type,
            type_label: TYPE_LABEL[type] || '电柜',
            capacity: capacity,
            capacity_raw: raw,
            capacity_split: String(raw).split(/[,，]+/).map((s) => parseInt(s, 10)).filter((n) => Number.isFinite(n)),
            chargeable: onlineValue ? effChargeable : 0,
            occupied: onlineValue ? occupied : 0,
            blocked: onlineValue ? blockedN : 0,
            fault: onlineValue ? fault : 0,
            online: onlineValue === 1,
            cabinet_status: onlineValue ? 'online' : 'offline',
            free_ratio: onlineValue && capacity ? effChargeable / capacity : 0,
            level: onlineValue ? levelOf(effChargeable, capacity, false) : 'offline',
            lng: (MOCK_COORDS[cabinetNo] && MOCK_COORDS[cabinetNo][0]) || meta.center.lng,
            lat: (MOCK_COORDS[cabinetNo] && MOCK_COORDS[cabinetNo][1]) || meta.center.lat,
            observed_at: new Date(nowMs - 8000).toISOString(),
            changed_at: new Date(nowMs - (hashStr(cabinetNo) % 40) * 60000).toISOString(),
            occupancy: occupancy
          };
          return cab;
        });

      const stats = siteStats(cabs);
      const offline = cabs.length > 0 && cabs.every((c) => !c.online);
      const level = offline ? 'offline' : levelOfRatio(stats.free_ratio, stats.ports_free, false);
      const forecast = FLAGS.cold ? coldForecast() : {
        median_charge_minutes: forc.median,
        samples: forc.samples,
        confidence: forc.confidence,
        expected_free_15m: forc.f15,
        expected_free_30m: forc.f30,
        expected_free_60m: forc.f60,
        next_free_in_minutes: forc.next
      };

      return {
        site_id: meta.site_id,
        name: meta.name,
        short: meta.short,
        raw_locations: meta.raw,
        center: meta.center,
        bounds: { minLng: 113.937, maxLng: 113.962, minLat: 22.798, maxLat: 22.809 },
        stats: stats,
        level: level,
        offline: offline,
        sample_scope: 'campus',
        cabinets: cabs,
        forecast: forecast
      };
    });

    return {
      server_time: new Date(nowMs).toISOString(),
      server_epoch_ms: nowMs,
      mode: 'public',
      mock: true,
      freshness: {
        interval_seconds: REFRESH_SECONDS,
        last_poll_at: new Date(nowMs - 8000).toISOString(),
        last_poll_ok: true,
        next_poll_in_seconds: REFRESH_SECONDS,
        stale: false
      },
      stats: totalStats(sites),
      duration_model: {
        median_minutes: PRIOR_MINUTES,
        observed_median_minutes: null,
        p25_minutes: null,
        p75_minutes: null,
        samples: 0,
        confidence: 'none',
        basis: 'prior',
        prior_minutes: PRIOR_MINUTES,
        note: '基于 chargeable 变化的历史推算，非平台官方数据'
      },
      sites: sites
    };
  }

  function coldForecast() {
    return {
      median_charge_minutes: null,
      samples: 0,
      confidence: 'none',
      expected_free_15m: null,
      expected_free_30m: null,
      expected_free_60m: null,
      next_free_in_minutes: null
    };
  }

  /* 点位级汇总：离线柜已从端口统计中剔除（chargeable 是历史残值，会虚报空位） */
  function siteStats(cabs) {
    let total = 0, free = 0, occ = 0, fault = 0, blocked = 0, precise = 0;
    let onlineCount = 0, offlineCount = 0;
    for (const c of cabs) {
      if (isOfflineCab(c)) { offlineCount++; continue; }
      onlineCount++;
      total += num(c.capacity);
      free += num(c.chargeable);
      occ += num(c.occupied);
      fault += num(c.fault);
      blocked += num(c.blocked);
      /* 精确剩余数只能从桩位明细里数（没有对应的一级字段） */
      const ports = (c.occupancy && c.occupancy.ports) || [];
      for (const p of ports) {
        if (p.remaining_is_exact) precise++;
      }
    }
    /* 🔴 blocked（infraed==1 未充电）的端口本来就不在平台 chargeable 里，
       所以 free 已经是真可用空位，不能再减一次 —— 否则明细会比地图少算一截。 */
    return {
      cabinets: onlineCount,
      cabinets_all: cabs.length,
      cabinets_online: onlineCount,
      cabinets_offline: offlineCount,
      ports_total: total,
      ports_free: free,
      ports_free_real: free,
      ports_occupied: occ,
      ports_fault: fault,
      ports_blocked: blocked,
      ports_precise: precise,
      free_ratio: total ? free / total : 0,
      occupancy_ratio: total ? occ / total : 0
    };
  }

  function totalStats(sites) {
    let total = 0, free = 0, occ = 0, fault = 0, blocked = 0, precise = 0;
    let onlineCount = 0, offlineCount = 0, allCount = 0;
    for (const s of sites) {
      const st = s.stats;
      total += num(st.ports_total);
      free += num(st.ports_free);
      occ += num(st.ports_occupied);
      fault += num(st.ports_fault);
      blocked += num(st.ports_blocked);
      precise += num(st.ports_precise);
      onlineCount += num(st.cabinets_online, num(st.cabinets));
      offlineCount += num(st.cabinets_offline);
      allCount += num(st.cabinets_all, num(st.cabinets));
    }
    return {
      cabinets: onlineCount,
      cabinets_all: allCount,
      cabinets_offline: offlineCount,
      ports_total: total,
      ports_free: free,
      ports_free_real: free,
      ports_occupied: occ,
      ports_fault: fault,
      ports_blocked: blocked,
      ports_precise: precise,
      free_ratio: total ? free / total : 0,
      sites: sites.length
    };
  }

  /* ------------------------------------------------------------------------
     4. 数据标准化（后端字段缺失时不让页面崩）
     ------------------------------------------------------------------------ */
  function normalizeState(raw) {
    if (!raw || typeof raw !== 'object') throw new Error('返回数据不是对象');
    const sites = Array.isArray(raw.sites) ? raw.sites : [];
    const normalized = sites.map((site) => {
      const cabs = Array.isArray(site.cabinets) ? site.cabinets.map(normalizeCabinet) : [];
      const computed = siteStats(cabs);
      const st = Object.assign({}, computed, site.stats || {});
      st.cabinets = num(st.cabinets, computed.cabinets);
      st.cabinets_all = num(st.cabinets_all, computed.cabinets_all);
      st.cabinets_online = num(st.cabinets_online, computed.cabinets_online);
      st.cabinets_offline = num(st.cabinets_offline, computed.cabinets_offline);
      st.ports_total = num(st.ports_total, computed.ports_total);
      st.ports_free = num(st.ports_free, computed.ports_free);
      st.ports_free_real = num(st.ports_free_real, computed.ports_free_real);
      st.ports_occupied = num(st.ports_occupied, computed.ports_occupied);
      st.ports_fault = num(st.ports_fault, computed.ports_fault);
      st.ports_blocked = num(st.ports_blocked, computed.ports_blocked);
      st.ports_precise = num(st.ports_precise, computed.ports_precise);
      st.free_ratio = Number.isFinite(Number(st.free_ratio))
        ? Number(st.free_ratio)
        : (st.ports_total ? st.ports_free_real / st.ports_total : 0);
      st.occupancy_ratio = Number.isFinite(Number(st.occupancy_ratio))
        ? Number(st.occupancy_ratio)
        : (st.ports_total ? st.ports_occupied / st.ports_total : 0);

      /* 离线判定：优先信后端 level==='offline'，其次「所有电柜都离线」 */
      const offline = site.level === 'offline'
        || (cabs.length > 0 && cabs.every((c) => c.online === false));
      const level = (site.level && LEVEL_TEXT[site.level])
        ? site.level
        : levelOfRatio(st.free_ratio, st.ports_free, offline);

      return {
        site_id: site.site_id || ('site-' + Math.random().toString(36).slice(2, 8)),
        name: site.name || site.site_id || '未命名点位',
        short: site.short || '',
        raw_locations: site.raw_locations || [],
        stats: st,
        level: level,
        offline: offline,
        sample_scope: site.sample_scope || 'campus',
        cabinets: cabs,
        forecast: site.forecast || null
      };
    });

    return {
      server_time: raw.server_time || null,
      server_epoch_ms: raw.server_epoch_ms || null,
      mode: raw.mode || 'public',
      mock: !!raw.mock,
      freshness: raw.freshness || null,
      stats: raw.stats || totalStats(normalized),
      duration_model: raw.duration_model || null,
      auth: raw.auth || null,
      sites: normalized
    };
  }

  function normalizeCabinet(cab) {
    const capacity = num(cab.capacity, capacitySum(cab.capacity_raw));
    const chargeable = num(cab.chargeable);
    const fault = num(cab.fault);
    /* blocked：平台 chargeable 不含这类端口（infraed==1 但未充电），
       后端从明细数出来，是"插着电池插不进去"的数量，与 free 并列不重叠。 */
    const blocked = num(cab.blocked);
    const occupied = Number.isFinite(Number(cab.occupied))
      ? num(cab.occupied)
      : Math.max(0, capacity - chargeable - fault - blocked);
    const online = cab.online === undefined ? true : !!cab.online;
    const isOffline = cab.level === 'offline' || !online;
    return {
      cabinet_no: cab.cabinet_no || '—',
      site_id: cab.site_id || '',
      cabinet_type: num(cab.cabinet_type),
      type_label: cab.type_label || TYPE_LABEL[num(cab.cabinet_type)] || '电柜',
      capacity: capacity,
      capacity_raw: cab.capacity_raw || String(capacity),
      chargeable: chargeable,
      occupied: occupied,
      blocked: blocked,
      fault: fault,
      online: online,
      cabinet_status: cab.cabinet_status || (online ? 'online' : 'offline'),
      free_ratio: Number.isFinite(Number(cab.free_ratio)) ? Number(cab.free_ratio) : (capacity ? chargeable / capacity : 0),
      level: (cab.level && LEVEL_TEXT[cab.level]) ? cab.level : levelOf(chargeable, capacity, isOffline),
      observed_at: cab.observed_at || null,
      changed_at: cab.changed_at || null,
      lng: Number.isFinite(Number(cab.lng)) ? Number(cab.lng) : null,
      lat: Number.isFinite(Number(cab.lat)) ? Number(cab.lat) : null,
      occupancy: normalizeOccupancy(cab.occupancy)
    };
  }

  /* 桩位：补齐 basis / estimate_basis / *_is_lower|upper_bound，端口缺失时返回 null
     source='platform_real' 表示逐桩位状态与已充时长来自平台真实数据，
     目标时长在 cs=6（到点断电）或 ct≥420 时可确定，此时剩余为精确值，
     否则为「不超过 480-ct」的上界；保留电量/充电状态等实测附加字段。 */
  function normalizeOccupancy(occ) {
    if (!occ || typeof occ !== 'object') return null;
    const isReal = occ.source === 'platform_real';
    const ports = Array.isArray(occ.ports) ? occ.ports.map((p, i) => ({
      port: p.port === undefined ? (i + 1) : p.port,
      state: p.state || 'unknown',
      elapsed_minutes: p.elapsed_minutes === undefined ? null : p.elapsed_minutes,
      elapsed_is_lower_bound: !!p.elapsed_is_lower_bound,
      remaining_minutes: p.remaining_minutes === undefined ? null : p.remaining_minutes,
      remaining_max_minutes: p.remaining_max_minutes === undefined ? null : p.remaining_max_minutes,
      remaining_is_upper_bound: !!p.remaining_is_upper_bound,
      remaining_is_exact: !!p.remaining_is_exact,
      target_minutes: p.target_minutes === undefined ? null : p.target_minutes,
      target_probability: p.target_probability === undefined ? null : p.target_probability,
      target_candidates: Array.isArray(p.target_candidates) ? p.target_candidates : null,
      end_at: p.end_at || null,
      physically_blocked: !!p.physically_blocked,
      firmware_status_text: p.firmware_status_text || null,
      energy_kwh: p.energy_kwh === undefined ? null : p.energy_kwh,
      median_minutes: p.median_minutes === undefined ? null : p.median_minutes,
      basis: p.basis || 'unknown',
      estimate_basis: p.estimate_basis || 'exponential_prior',
      source: isReal && p.source === 'platform_real' ? 'platform_real' : (p.source || null),
      cabox_id: p.cabox_id || null,
      charger_status_text: p.charger_status_text || null,
      battery_type: p.battery_type || null,
      percentage: p.percentage === undefined ? null : p.percentage,
      infraed: p.infraed === undefined ? null : p.infraed,
      voltage: p.voltage === undefined ? null : p.voltage,
      ampere: p.ampere === undefined ? null : p.ampere,
      start_time: p.start_time || null,
      end_time: p.end_time || null
    })) : [];
    return {
      estimated: isReal ? false : occ.estimated !== false,
      offline: !!occ.offline,
      source: occ.source || (isReal ? 'platform_real' : 'estimated'),
      ports: ports,
      note: occ.note || (isReal
        ? '逐桩位状态与已充时长来自平台真实数据；目标时长已知时剩余为精确值，否则为上界'
        : '桩位号按 chargeable 变化顺序推定，非真实物理编号')
    };
  }

  /* ------------------------------------------------------------------------
     5. 渲染 · 概览大数字
     ------------------------------------------------------------------------ */
  function renderOverview(state) {
    const st = state.stats || {};
    const ratio = Number.isFinite(Number(st.free_ratio)) ? Number(st.free_ratio) : 0;
    const cabsOnline = num(st.cabinets);
    const cabsAll = num(st.cabinets_all, cabsOnline);
    const cabsOffline = num(st.cabinets_offline, Math.max(0, cabsAll - cabsOnline));
    clear(dom.statRow);

    const items = [
      {
        label: '空闲桩位',
        value: num(st.ports_free_real, st.ports_free),
        unit: '个',
        note: '可立即开始充电的桩位总数',
        accent: true
      },
      {
        label: '全场桩位',
        value: num(st.ports_total),
        unit: '位',
        note: (cabsOffline > 0
          ? cabsOnline + '/' + cabsAll + ' 台电柜在线（' + cabsOffline + ' 台离线已剔除）· '
          : cabsOnline + ' 台电柜 · ') + num(st.sites || state.sites.length) + ' 个点位'
      },
      {
        label: '空闲率',
        value: Math.round(ratio * 100),
        unit: '%',
        note: '空闲 ÷ 全场；> 40% 为充裕'
      },
      {
        label: '使用中',
        value: num(st.ports_occupied),
        unit: '个',
        note: '正在充电，时长由历史推算'
      },
      {
        label: '故障桩位',
        value: num(st.ports_fault),
        unit: '个',
        note: cabsOffline > 0
          ? '上报异常的桩位；另有 ' + cabsOffline + ' 台电柜离线，状态未知'
          : '上报为异常的空桩位，不参与可用统计'
      }
    ];

    for (const item of items) {
      const value = el('p', { class: 'stat__value' }, [
        document.createTextNode(String(item.value)),
        el('span', { class: 'stat__unit', text: item.unit })
      ]);
      dom.statRow.append(el('div', { class: 'stat' + (item.accent ? ' stat--accent' : '') }, [
        el('p', { class: 'stat__label', text: item.label }),
        value,
        el('p', { class: 'stat__note', text: item.note })
      ]));
    }

    if (dom.deckCabinets) dom.deckCabinets.textContent = String(num(st.cabinets));
  }

  /* 要点：从数据里挑 4 条真正有用的结论 */
  function renderInsights(state) {
    clear(dom.insights);
    const sites = state.sites || [];
    if (!sites.length) return;

    const byFree = sites.filter((s) => !s.offline && s.stats.ports_total > 0)
      .sort((a, b) => num(b.stats.ports_free_real, b.stats.ports_free) - num(a.stats.ports_free_real, a.stats.ports_free));
    const best = byFree[0];
    /* 只在线上的点位里挑「最需要排队」——离线点位不能代表「满位」 */
    const onlineSites = sites.filter((s) => !s.offline && s.stats.ports_total > 0);
    const tight = onlineSites.slice().sort((a, b) => a.stats.free_ratio - b.stats.free_ratio)[0];
    const totalFault = num((state.stats || {}).ports_fault);
    const cabsAll = num((state.stats || {}).cabinets_all);
    const cabsOffline = num((state.stats || {}).cabinets_offline);

    const push = (label, nodes, accent) => {
      const li = el('li', { class: 'insight' });
      if (accent) li.dataset.tone = 'accent';
      const bar = el('span', { class: 'insight__bar', 'aria-hidden': 'true' });
      li.append(bar, el('div', {}, [
        el('span', { class: 'insight__k', text: label }),
        el('p', { class: 'insight__v' }, nodes)
      ]));
      dom.insights.append(li);
    };

    if (best) {
      push('现在最空', [
        el('b', { text: best.name }),
        document.createTextNode(' 有 ' + num(best.stats.ports_free_real, best.stats.ports_free) + ' 个空闲桩位，空闲率 ' + Math.round(pct(best.stats.free_ratio)) + '%')
      ], true);
    } else {
      push('现在最空', [document.createTextNode('所有点位电柜都处于离线状态，暂无实时空闲数据')], true);
    }

    if (tight) {
      const isTight = tight.level === 'none' || tight.level === 'low';
      push('最需要排队', [
        el('b', { text: tight.name }),
        document.createTextNode(
          isTight
            ? ' 仅 ' + num(tight.stats.ports_free_real, tight.stats.ports_free) + ' 个空位（' + Math.round(pct(tight.stats.free_ratio)) + '%），已达「' + LEVEL_TEXT[tight.level] + '」'
            : ' 空闲 ' + num(tight.stats.ports_free_real, tight.stats.ports_free) + ' 个（' + Math.round(pct(tight.stats.free_ratio)) + '%），尚有余量'
        )
      ], false);
    }

    if (totalFault > 0 || cabsOffline > 0) {
      const parts = [];
      if (totalFault > 0) {
        const share = num((state.stats || {}).ports_total) ? Math.round(totalFault / num(state.stats.ports_total) * 100) : 0;
        parts.push(el('b', { text: String(totalFault) }));
        parts.push(document.createTextNode(' 个桩位上报异常，占全场 ' + share + '%，不算进空闲'));
      }
      if (cabsOffline > 0) {
        if (parts.length) parts.push(document.createTextNode('；'));
        parts.push(el('b', { text: String(cabsOffline) }));
        parts.push(document.createTextNode(' 台电柜离线' + (cabsAll > 0 ? '（共 ' + cabsAll + ' 台）' : '') + '，其桩位状态未知、已从统计中剔除'));
      }
      push('故障与离线', parts, false);
    }

    const soon = sites
      .filter((s) => s.forecast && s.forecast.next_free_in_minutes !== null && s.forecast.next_free_in_minutes !== undefined)
      .sort((a, b) => num(a.forecast.next_free_in_minutes) - num(b.forecast.next_free_in_minutes))[0];
    if (soon) {
      push('最快出现空位', [
        el('b', { text: soon.name }),
        document.createTextNode(' 预计 ' + fmtDuration(soon.forecast.next_free_in_minutes) + ' 内出现空位（推算）')
      ], false);
    } else {
      push('空出预测', [document.createTextNode('各点位仍在积累完整充电记录，暂无法推算空出时间')], false);
    }
  }

  /* ------------------------------------------------------------------------
     6. 渲染 · 空闲率对比条
     ------------------------------------------------------------------------ */
  /* ------------------------------------------------------------------------
      6.5 渲染 · 电柜地图（Leaflet + 高德 GCJ-02 瓦片）
      —— 与小程序同款：平台给 WGS84 坐标，先转 GCJ02 再落到底图；
         气泡数字 = 该组可用桩数，颜色按柜型，离线灰。点击气泡跳到点位明细。
      ------------------------------------------------------------------------ */
  const MAP_TYPES = {
    1: { color: '#327BFA', label: '换电柜' },
    2: { color: '#44CF89', label: '充电柜' },
    3: { color: '#E3AE00', label: '充电桩' },
    4: { color: '#9586F7', label: '汽车桩' },
    5: { color: '#44CF89', label: '混合柜' }
  };
  const CAMPUS_CENTER = [22.8036, 113.9457];
  const CAMPUS_ZOOM = 15;
  let mapObj = null;
  let markerLayer = null;

  /* 与小程序 app-service.js 模块 7c94 的 transformFromWGSToGCJ 完全一致 */
  function wgs84ToGcj02(lat, lng) {
    const pi = Math.PI, a = 6378245.0, ee = 0.006693421622965943;
    function outOfChina(lat, lng) { return !(lng > 73.66 && lng < 135.05 && lat > 3.86 && lat < 53.55); }
    function transLat(x, y) {
      let ret = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
      ret += (20 * Math.sin(6 * x * pi) + 20 * Math.sin(2 * x * pi)) * 2 / 3;
      ret += (20 * Math.sin(y * pi) + 40 * Math.sin(y / 3 * pi)) * 2 / 3;
      ret += (160 * Math.sin(y / 12 * pi) + 320 * Math.sin(y * pi / 30)) * 2 / 3;
      return ret;
    }
    function transLng(x, y) {
      let ret = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
      ret += (20 * Math.sin(6 * x * pi) + 20 * Math.sin(2 * x * pi)) * 2 / 3;
      ret += (20 * Math.sin(x * pi) + 40 * Math.sin(x / 3 * pi)) * 2 / 3;
      ret += (150 * Math.sin(x / 12 * pi) + 300 * Math.sin(x / 30 * pi)) * 2 / 3;
      return ret;
    }
    lat = +lat; lng = +lng;
    if (outOfChina(lat, lng)) return [lat, lng];
    let dLat = transLat(lng - 105, lat - 35);
    let dLng = transLng(lng - 105, lat - 35);
    const radLat = lat / 180 * pi;
    let magic = Math.sin(radLat);
    magic = 1 - ee * magic * magic;
    const sqrtMagic = Math.sqrt(magic);
    dLat = (dLat * 180) / ((a * (1 - ee)) / (magic * sqrtMagic) * pi);
    dLng = (dLng * 180) / (a / sqrtMagic * Math.cos(radLat) * pi);
    return [lat + dLat, lng + dLng];
  }

  /* GCJ02 → WGS84 反向解算（迭代逼近，与小程序 7c94.transformFromGCJToWGS 同思路）。
     用途：从高德底图切回 WGS84 底图时，把当前视野中心换回原始坐标，避免视野跳动。 */
  function gcj02ToWgs84(lat, lng) {
    let wgsLat = lat, wgsLng = lng;
    for (let i = 0; i < 5; i++) {
      const fwd = wgs84ToGcj02(wgsLat, wgsLng);
      const dLat = fwd[0] - lat;
      const dLng = fwd[1] - lng;
      wgsLat -= dLat;
      wgsLng -= dLng;
      if (Math.abs(dLat) < 1e-9 && Math.abs(dLng) < 1e-9) break;
    }
    return [wgsLat, wgsLng];
  }

  /* 校园中心来自采集配置，是 WGS84。 */

  /* ------------------------------------------------------------------------
     底图与坐标口径（datum）
     —— 平台返回的经纬度经 OSM 底图实测为 **WGS84**（西园4栋原始坐标正落在
        西园6宿舍/农业银行一带的真实楼群上）；而高德瓦片是 GCJ02，需转换后
        才能对齐，否则整体偏约 600m。所以「底图坐标系」决定 marker 是否转换：
        datum='wgs84' → 直接用平台原始坐标；datum='gcj02' → 先转 GCJ02。
     —— 默认用 OSM/Carto（WGS84 实测数据，校园建筑轮廓准），
        另备高德（国内路网注记更全）与 Esri 卫星图。
     ------------------------------------------------------------------------ */
  const BASE_LAYERS = {
    osm: {
      label: '街道图',
      datum: 'wgs84',
      url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      opts: { maxZoom: 19, attribution: '© OpenStreetMap contributors' }
    },
    amap: {
      label: '高德图',
      datum: 'gcj02',
      url: 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
      opts: { subdomains: '1234', maxZoom: 18, attribution: '地图数据 © 高德地图' },
      /* 实测：高德瓦片在本片区（光明区公常路一带）自带约 150–200m 系统性偏移，
         同一坐标在 OSM/卫星图上落在宿舍楼群、在高德图上却落在校外商铺。
         保留该底图只为看中文路网注记，切过去时明确提示位置不可用于定位。 */
      warn: '高德底图在本片区自带约 150–200m 偏移，仅可参考路网与地名，气泡位置请以街道图/卫星图为准。'
    },
    satellite: {
      label: '卫星图',
      datum: 'wgs84',
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      opts: { maxZoom: 19, attribution: '影像 © Esri, Maxar, Earthstar Geographics' }
    }
  };
  const BASE_ORDER = ['osm', 'amap', 'satellite'];
  let baseKey = 'osm';
  let tileLayerObj = null;

  /* 按当前底图坐标系把平台 WGS84 坐标投到地图上 */
  function projectPos(lat, lng) {
    return BASE_LAYERS[baseKey].datum === 'gcj02' ? wgs84ToGcj02(lat, lng) : [+lat, +lng];
  }
  /* 深链接/复位用的中心点（同样跟随底图坐标系） */
  function projectCenter(lat, lng) {
    return BASE_LAYERS[baseKey].datum === 'gcj02' ? wgs84ToGcj02(lat, lng) : [+lat, +lng];
  }

  function applyTileLayer() {
    if (!mapObj) return;
    if (tileLayerObj) mapObj.removeLayer(tileLayerObj);
    const cfg = BASE_LAYERS[baseKey];
    tileLayerObj = L.tileLayer(cfg.url, cfg.opts).addTo(mapObj);
    if (tileLayerObj.bringToBack) tileLayerObj.bringToBack();
    if (dom.mapBase) dom.mapBase.textContent = cfg.label;
    // 底图自带偏移时，把告警显示在图下注记处
    if (dom.mapNote) {
      dom.mapNote.textContent = cfg.warn
        ? '⚠ ' + cfg.warn
        : '气泡数字 = 该组可用桩数，灰色 0 空位、彩色有空位、红色离线。气泡钉在该组真实经纬度上（WGS84），缩放平移不漂移。点击气泡跳到对应组查看逐桩位。';
      dom.mapNote.classList.toggle('map-note--warn', !!cfg.warn);
    }
  }

  function initMap() {
    if (mapObj || !dom.mapEl || typeof L === 'undefined') return;
    /* 可选深链接：?view=lat,lng,zoom —— 与平台数据同为 **WGS84**，
       内部按当前底图坐标系自动投影，例如 ?view=22.8017,113.9391,18 聚焦西园4栋。 */
    let viewWgs = null;
    let zoom = CAMPUS_ZOOM;
    const view = (params.get('view') || '').split(',').map((s) => parseFloat(s));
    if (view.length === 3 && view.every((n) => Number.isFinite(n))) {
      viewWgs = [Math.max(-85, Math.min(85, view[0])), view[1]];
      zoom = Math.max(10, Math.min(19, Math.round(view[2])));
    }
    const center = viewWgs ? projectCenter(viewWgs[0], viewWgs[1]) : projectCenter(CAMPUS_CENTER[0], CAMPUS_CENTER[1]);
    mapObj = L.map(dom.mapEl, {
      center: center,
      zoom: zoom,
      zoomControl: true,
      scrollWheelZoom: true,
      attributionControl: true
    });
    applyTileLayer();
    markerLayer = L.layerGroup().addTo(mapObj);
    if (dom.mapReset) dom.mapReset.addEventListener('click', () => {
      mapObj.flyTo(projectCenter(CAMPUS_CENTER[0], CAMPUS_CENTER[1]), CAMPUS_ZOOM, { duration: 0.6 });
    });
    /* 底图轮换：街道图(OSM/WGS84) → 高德图(GCJ02) → 卫星图(WGS84)。
       切到底图坐标系不同的图层时，marker 与视野中心都要重新投影，否则会整体偏移。 */
    if (dom.mapBase) dom.mapBase.addEventListener('click', () => {
      const c = mapObj.getCenter();
      const z = mapObj.getZoom();
      // 先把当前屏幕中心换算回 WGS84，切换后按新坐标系重新投影，保持视野不跳
      const wgs = BASE_LAYERS[baseKey].datum === 'gcj02' ? gcj02ToWgs84(c.lat, c.lng) : [c.lat, c.lng];
      const i = BASE_ORDER.indexOf(baseKey);
      baseKey = BASE_ORDER[(i + 1) % BASE_ORDER.length];
      applyTileLayer();
      renderMap(runtime.lastPayload);
      mapObj.setView(projectCenter(wgs[0], wgs[1]), z, { animate: false });
    });
  }

  /* 严格复刻小程序地图 marker：
     —— 坐标先由 WGS84 转 GCJ02 后钉在真实地理点上（与小程序 getMarkers 一致）；
     —— 图标直接用平台真实的圆形气泡图（自带数字 + 白边 + 阴影）：
        0 空位=灰泡 none0，N 空位=按柜型彩色泡(cdz/cdg/hdg/qcz/gz)，离线=红泡 outLine0；
     —— 平台 48 台柜没有任何两个坐标完全相同，按小程序逻辑不做人为散开，
        图标按真实坐标自然叠放成斜排（正是小程序里几组柜叠成一排的效果）；
        Leaflet 按地理坐标投影，缩放/平移时始终钉在正确位置，不漂移。 */
  const MARKER_MAX_NUM = 20; // 平台图标库每类数字泡 0..20
  function markerIcon(cab) {
    const free = num(cab.chargeable);
    const offline = !cab.online;
    const type = +cab.cabinet_type;
    let name;
    if (offline) {
      name = 'outLine0';              // 离线 → 红泡
    } else if (free <= 0) {
      name = 'none0';                 // 无空位 → 灰泡
    } else {
      let prefix = 'cdz';             // 3 充电桩 / 5 混合柜 → 橙泡 cdz
      if (type === 1) prefix = 'gz';   // 换电柜 → 蓝泡 gz
      else if (type === 2) prefix = 'cdg'; // 充电柜 → 绿泡 cdg
      else if (type === 4) prefix = 'qcz'; // 汽车桩 → 青泡 qcz
      const n = Math.min(Math.max(1, free), MARKER_MAX_NUM);
      name = prefix + n;
    }
    return L.icon({
      iconUrl: 'vendor/markers/' + name + '.png',
      iconSize: [40, 42],
      iconAnchor: [20, 21],           // 图标中心对齐真实坐标点，相邻柜自然重叠成斜排
      popupAnchor: [0, -18],
      className: 'mk-icon'
    });
  }

  function renderMap(state) {
    initMap();
    if (!mapObj || !markerLayer) return;
    markerLayer.clearLayers();
    const sites = state.sites || [];
    const allCabs = [];
    for (const site of sites) for (const cab of (site.cabinets || [])) allCabs.push({ site, cab });
    // 离线柜放底层、在线柜放上层，避免灰泡盖住有空位的彩泡（与小程序 zIndex 逻辑一致）
    allCabs.sort((a, b) => (a.cab.online === b.cab.online) ? num(b.cab.chargeable) - num(a.cab.chargeable) : (a.cab.online ? 1 : -1));
    for (const { site, cab } of allCabs) {
      if (!Number.isFinite(+cab.lat) || !Number.isFinite(+cab.lng)) continue;
      const pos = projectPos(cab.lat, cab.lng);
      const marker = L.marker(pos, {
        icon: markerIcon(cab),
        title: cab.cabinet_no + ' · ' + (cab.online ? num(cab.chargeable) + ' 空位' : '离线')
      });
      marker.bindPopup(
        '<div class="mk-pop"><b>' + escapeHtml(cab.cabinet_no) + '</b>' +
        '<span>' + escapeHtml(site.name) + ' · ' + escapeHtml(cab.type_label || (MAP_TYPES[cab.cabinet_type] || {}).label || '电柜') + '</span>' +
        '<span>' + (cab.online ? '可用 <b>' + num(cab.chargeable) + '</b> / ' + num(cab.capacity) + ' 桩位' : '离线 · 状态未知') + '</span></div>'
      );
      marker.on('click', () => jumpToGroup(site.site_id, cab.cabinet_no));
      markerLayer.addLayer(marker);
    }
  }

  /* 点击地图气泡 → 展开对应点位明细并选中该组 */
  function jumpToGroup(siteId, cabinetNo) {
    const card = document.querySelector('.site-card[aria-labelledby="site-' + siteId + '"]');
    if (!card) return;
    const more = card.querySelector('.site-more');
    if (more && !more.open) more.open = true;
    UI_STATE[siteId] = { cab: cabinetNo, port: null };
    // 用最新 payload 局部重绘该点位的分组区
    const wrap = card.querySelector('.grp');
    if (wrap) reRenderGroup(siteId, wrap);
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  }

  function renderCompare(state) {
    clear(dom.compareList);
    const sites = (state.sites || []).slice().sort((a, b) => b.stats.free_ratio - a.stats.free_ratio);

    if (!sites.length) {
      dom.compareList.append(emptyState('暂无点位数据', '接口返回的 sites 为空。'));
      return;
    }

    for (const site of sites) {
      const offline = !!site.offline;
      const ratio = Math.max(0, Math.min(1, num(site.stats.free_ratio)));
      const row = el('div', {
        class: 'cmp-row' + (offline ? ' cmp-row--offline' : ''),
        dataset: { level: site.level, offline: offline ? 'true' : 'false' }
      }, [
        el('span', { class: 'cmp-row__name' }, [
          el('span', { text: site.name }),
          el('span', { class: 'cmp-row__lv', text: LEVEL_TEXT[site.level] || '—' })
        ]),
        el('div', { class: 'cmp-row__track' }, [
          el('div', { class: 'cmp-row__fill', style: 'width:' + (offline ? 100 : pct(ratio)).toFixed(1) + '%' }),
          el('i', { class: 'cmp-row__grid', dataset: { at: '15' }, 'aria-hidden': 'true' }),
          el('i', { class: 'cmp-row__grid', dataset: { at: '40' }, 'aria-hidden': 'true' }),
          el('span', { class: 'cmp-row__free', text: offline ? '—' : String(num(site.stats.ports_free_real, site.stats.ports_free)) })
        ]),
        el('span', { class: 'cmp-row__val' }, [
          document.createTextNode(offline ? '离线 ' : Math.round(pct(ratio)) + '% '),
          el('small', { text: offline ? '无实时读数' : num(site.stats.ports_free_real, site.stats.ports_free) + '/' + site.stats.ports_total })
        ])
      ]);
      dom.compareList.append(row);
    }

    dom.compareList.append(el('div', { class: 'cmp-axis', 'aria-hidden': 'true' }, [
      el('span', {}),
      el('div', { class: 'cmp-axis__ticks' }, [
        el('span', { dataset: { at: '0' }, text: '0' }),
        el('span', { dataset: { at: '15' }, text: '15% 紧张线' }),
        el('span', { dataset: { at: '40' }, text: '40% 充裕线' }),
        el('span', { dataset: { at: '100' }, text: '100%' })
      ]),
      el('span', {})
    ]));
  }

  /* ------------------------------------------------------------------------
     7. 渲染 · 点位卡片
     ------------------------------------------------------------------------ */
  /* ------------------------------------------------------------------------
      区域空位查询：选区域 → 列出全部空闲桩位；无空位时列出最快空出的 3–5 个
      ------------------------------------------------------------------------ */
  const FINDER_TOP = 5;              // 无空位时展示的「最快空出」桩位数量上限
  let finderSelectedSiteId = null;   // 当前选中的区域，跨刷新保留
  let finderExpanded = false;        // 详情区是否展开（默认折叠，只留区域选择行）

  /* 折叠 / 展开详情区：默认只显示图二的区域选择条，避免长列表刷屏 */
  function applyFinderCollapse() {
    if (dom.finderRoot) dom.finderRoot.classList.toggle('is-collapsed', !finderExpanded);
    if (dom.finderToggle) {
      dom.finderToggle.setAttribute('aria-expanded', finderExpanded ? 'true' : 'false');
      const label = dom.finderToggle.querySelector('.finder__toggle-label');
      if (label) label.textContent = finderExpanded ? '收起详情' : '展开详情';
      const arrow = dom.finderToggle.querySelector('.finder__toggle-arrow');
      if (arrow) arrow.textContent = finderExpanded ? '▴' : '▾';
    }
  }

  /* 摊平某区域所有桩位，附带所属电柜，便于按组聚合 */
  function finderPorts(site) {
    const out = [];
    for (const cab of site.cabinets || []) {
      const occ = cab.occupancy;
      if (!occ || !Array.isArray(occ.ports)) continue;
      for (const p of occ.ports) out.push({ cab: cab, port: p });
    }
    return out;
  }

  function renderFinder(state) {
    if (!dom.finderChips || !dom.finderResult) return;
    const sites = (state.sites || []).slice().sort((a, b) => {
      const af = num(a.stats.ports_free_real, a.stats.ports_free), bf = num(b.stats.ports_free_real, b.stats.ports_free);
      if (bf !== af) return bf - af;
      return a.name.localeCompare(b.name, 'zh-Hans-CN');
    });
    if (!sites.length) {
      clear(dom.finderChips);
      clear(dom.finderResult);
      dom.finderResult.append(el('p', { class: 'finder__placeholder', text: '暂无区域数据。' }));
      return;
    }
    if (!sites.some((s) => s.site_id === finderSelectedSiteId)) finderSelectedSiteId = sites[0].site_id;

    /* 区域选择 chips */
    clear(dom.finderChips);
    for (const site of sites) {
      const ports = finderPorts(site);
      const freeN = ports.filter((x) => x.port.state === 'free').length;
      const active = site.site_id === finderSelectedSiteId;
      const chip = el('button', {
        class: 'finder__chip' + (active ? ' is-active' : ''),
        type: 'button',
        'aria-pressed': active ? 'true' : 'false',
        dataset: { site: site.site_id }
      }, [
        el('span', { class: 'finder__chip-name', text: site.short || site.name }),
        el('span', {
          class: 'finder__chip-num' + (site.offline ? ' is-offline' : (freeN ? '' : ' is-empty')),
          text: site.offline ? '离线' : (freeN + ' 空')
        })
      ]);
      chip.addEventListener('click', () => { finderSelectedSiteId = site.site_id; renderFinder(state); });
      dom.finderChips.append(chip);
    }

    renderFinderResult(sites.find((s) => s.site_id === finderSelectedSiteId));
  }

  function renderFinderResult(site) {
    clear(dom.finderResult);
    if (!site) return;
    const all = finderPorts(site);
    const geo = geoOrderCabs((site.cabinets || []).filter((c) => !isOfflineCab(c)));
    const groupOf = (cab) => { const g = geo.get(cab.cabinet_no); return g && g.pos ? ('第' + g.pos + '组') : '—'; };

    if (site.offline) {
      dom.finderResult.append(el('div', { class: 'finder__offline' }, [
        el('p', { class: 'finder__lead', text: site.name + ' · 当前离线' }),
        el('p', { class: 'finder__sub', text: '该区域电柜失联，桩位状态未知，无法查询空闲。恢复在线后自动更新。' })
      ]));
      return;
    }

    /* 有空闲：全部列出，按组聚合 */
    const free = all.filter((x) => x.port.state === 'free');
    if (free.length) {
      dom.finderResult.append(el('div', { class: 'finder__summary finder__summary--free' }, [
        el('span', { class: 'finder__summary-num', text: String(free.length) }),
        el('span', { class: 'finder__summary-unit', text: '个空闲桩位' }),
        el('span', { class: 'finder__summary-note', text: site.name + ' · 可立即前往充电' })
      ]));
      const byGroup = new Map();
      for (const x of free) {
        const k = groupOf(x.cab);
        if (!byGroup.has(k)) byGroup.set(k, []);
        byGroup.get(k).push(x);
      }
      /* 按现场组号排序，让列表顺序 = 从一端到另一端的实际顺序 */
      const sortedGroups = [...byGroup.keys()].sort((a, b) => {
        const na = parseInt(String(a).replace(/\D/g, ''), 10) || 0;
        const nb = parseInt(String(b).replace(/\D/g, ''), 10) || 0;
        return na - nb;
      });
      const list = el('div', { class: 'finder__freelist' });
      for (const g of sortedGroups) {
        const items = byGroup.get(g);
        const row = el('div', { class: 'finder__frowser' }, [
          el('span', { class: 'finder__group', text: g }),
          el('div', { class: 'finder__ports' })
        ]);
        const box = row.querySelector('.finder__ports');
        const sortedPorts = items.slice().sort((a, b) => num(a.port.port) - num(b.port.port));
        for (const x of sortedPorts) {
          box.append(el('span', { class: 'finder__port finder__port--free' }, [
            el('b', { text: x.cab.cabinet_no }),
            el('span', { text: '#' + x.port.port + ' 号' })
          ]));
        }
        list.append(row);
      }
      dom.finderResult.append(list);
      return;
    }

    /* 无空闲：按剩余时间升序取最快的 3–5 个 */
    const occupied = all.filter((x) => x.port.state === 'occupied'
      && x.port.remaining_minutes !== null && x.port.remaining_minutes !== undefined);
    occupied.sort((a, b) => num(a.port.remaining_minutes) - num(b.port.remaining_minutes));
    const top = occupied.slice(0, FINDER_TOP);
    dom.finderResult.append(el('div', { class: 'finder__summary finder__summary--none' }, [
      el('span', { class: 'finder__summary-num', text: '0' }),
      el('span', { class: 'finder__summary-unit', text: '个空闲桩位' }),
      el('span', { class: 'finder__summary-note', text: site.name + ' · 以下为最快空出的桩位' })
    ]));
    if (!top.length) {
      dom.finderResult.append(el('p', { class: 'finder__sub', text: '当前没有可推算空出时间的占用桩位，请稍后再看或切换其他区域。' }));
      return;
    }
    const list = el('ol', { class: 'finder__waitlist' });
    for (let i = 0; i < top.length; i++) {
      const x = top[i], p = x.port;
      const isReal = p.source === 'platform_real';
      list.append(el('li', { class: 'finder__waitrow' }, [
        el('span', { class: 'finder__rank', text: String(i + 1) }),
        el('div', { class: 'finder__waitmain' }, [
          el('div', { class: 'finder__waitwhere' }, [
            el('span', { class: 'finder__group', text: groupOf(x.cab) }),
            el('b', { text: x.cab.cabinet_no }),
            el('span', { text: '#' + p.port + ' 号桩' }),
            isReal ? el('span', { class: 'finder__real', text: '实测' }) : null
          ]),
          el('div', {
            class: 'finder__waitmeta',
            text: '已充 ' + fmtDuration(p.elapsed_minutes) + (p.end_at ? ' · 预计 ' + fmtClock(p.end_at) + ' 空出' : '')
          })
        ]),
        el('div', { class: 'finder__waitremain' }, [
          el('span', { class: 'finder__waitremain-label', text: '还需' }),
          el('b', { text: fmtRemaining(p.remaining_minutes) })
        ])
      ]));
    }
    dom.finderResult.append(list);
    dom.finderResult.append(el('p', { class: 'finder__foot', text: '「还需」按充电时长历史推算（标「实测」者更精确），用户可能提前结束而空出。' }));
  }

  /* ------------------------------------------------------------------------
       每日桩位趋势：选日期 + 选区域 → 可用 / 占用折线（纯 SVG 自绘，无依赖）
       数据来自 GET /api/site-history?site=&day=YYYY-MM-DD（自然日窗口，
       X 轴恒定 0:00–23:59）；日期列表来自 GET /api/history-days?days=7。
       mock 模式用确定性波形。
       ------------------------------------------------------------------------ */
  const TREND_DAYS = 7;              // 日期栏保留最近 7 天
  let trendSelectedSiteId = 'all';   // 当前选中的区域（all = 全校区），跨刷新保留
  let trendSelectedDay = null;       // 当前选中的自然日 'YYYY-MM-DD'，null = 今天
  let trendMetric = 'free';          // 当前显示的曲线：free 可用 / occupied 占用
  let trendDays = [];                // [{day,label,samples,is_today,complete}]
  let trendDaysExpanded = false;     // 日期栏是否展开
  let trendLastState = null;         // 最近一次 /api/state，供日期切换时重绘
  const trendCache = Object.create(null); // site+day -> {points, cabinets, day}

  function todayKey() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function trendDay() {
    return trendSelectedDay || todayKey();
  }

  function trendCacheKey() {
    return trendSelectedSiteId + '@' + trendDay();
  }

  /* 为 mock 造一条固定落在当天 0:00–23:59 的确定性折线（144 点，每 10 分钟） */
  function mockTrendPoints(state, siteId, day) {
    const sites = state.sites || [];
    const target = siteId === 'all' ? null : sites.find((s) => s.site_id === siteId);
    let total = 0;
    if (target) {
      total = num((target.stats || {}).ports_total, 0);
    } else {
      for (const s of sites) total += num((s.stats || {}).ports_total, 0);
    }
    if (!total) total = 600;
    // 当天 00:00 的本地时刻作为轴起点
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day || todayKey());
    const base = m
      ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 0, 0, 0).getTime()
      : new Date().setHours(0, 0, 0, 0);
    const pts = [];
    let seed = 0;
    for (const s of sites) for (const ch of String(s.site_id || '')) seed += ch.charCodeAt(0);
    for (let i = 0; i < 144; i++) {
      const phase = seed % 17;
      const wave = Math.sin((i + phase) / 9) * 0.13 + Math.sin((i + phase) / 23) * 0.08;
      // 昼夜项：i/144 即当天进度，夜里占用高、可用低
      const dayWave = -0.16 * Math.sin((i / 144) * Math.PI * 2 + 1.2);
      const noise = (Math.sin(i * 12.9898 + phase) * 43758.5453 % 1) * 0.04;
      let freeRatio = 0.32 + wave + dayWave + noise;
      freeRatio = Math.max(0.03, Math.min(0.85, freeRatio));
      pts.push({
        t: new Date(base + i * 10 * 60 * 1000).toISOString(),
        free: Math.round(total * freeRatio),
        occupied: Math.round(total * (1 - freeRatio)),
        cabinets: target ? num((target.stats || {}).cabinets, 10) : 48
      });
    }
    return pts;
  }

  function mockTrendDays() {
    const out = [];
    const now = new Date();
    for (let i = 0; i < TREND_DAYS; i++) {
      const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
      const pad = (n) => String(n).padStart(2, '0');
      const key = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
      out.push({
        day: key,
        label: i === 0 ? '今天' : (i === 1 ? '昨天' : (d.getMonth() + 1) + '/' + d.getDate()),
        samples: i === 2 ? 0 : 8000,
        is_today: i === 0,
        complete: i !== 0
      });
    }
    return out;
  }

  function trendAreas(state) {
    const sites = (state.sites || []).slice().sort((a, b) =>
      (b.stats.ports_total || 0) - (a.stats.ports_total || 0));
    const list = [{ site_id: 'all', short: '全校区' }].concat(
      sites.filter((s) => !(s.offline)).map((s) => ({ site_id: s.site_id, short: s.short || s.name }))
    );
    if (!list.some((x) => x.site_id === trendSelectedSiteId)) trendSelectedSiteId = 'all';
    return list;
  }

  function renderTrendAreas(state) {
    if (!dom.trendAreas) return;
    clear(dom.trendAreas);
    for (const area of trendAreas(state)) {
      const active = area.site_id === trendSelectedSiteId;
      const btn = el('button', {
        class: 'trend__area' + (active ? ' is-active' : ''),
        type: 'button',
        'aria-pressed': active ? 'true' : 'false',
        dataset: { site: area.site_id }
      }, [el('span', { text: area.short })]);
      btn.addEventListener('click', () => {
        if (area.site_id === trendSelectedSiteId) return;
        trendSelectedSiteId = area.site_id;
        renderTrendAreas(state);
        loadTrendDays(state);
        loadTrend(state, true);
      });
      dom.trendAreas.append(btn);
    }
  }

  /* 日期栏：折叠成一行「选择日期 [今天] ▾」，展开列出最近 7 天 */
  function applyTrendDayCollapse() {
    if (!dom.trendDaybar) return;
    dom.trendDaybar.classList.toggle('is-collapsed', !trendDaysExpanded);
    if (dom.trendDaytoggle) {
      dom.trendDaytoggle.setAttribute('aria-expanded', trendDaysExpanded ? 'true' : 'false');
      const label = dom.trendDaytoggle.querySelector('.trend__daytoggle-label');
      if (label) label.textContent = trendDaysExpanded ? '收起日期' : '选择日期';
      const arrow = dom.trendDaytoggle.querySelector('.trend__daytoggle-arrow');
      if (arrow) arrow.textContent = trendDaysExpanded ? '▴' : '▾';
    }
    if (dom.trendDaycur) {
      const hit = trendDays.find((d) => d.day === trendDay());
      dom.trendDaycur.textContent = hit ? hit.label : (trendSelectedDay || '今天');
    }
  }

  function renderTrendDays() {
    if (!dom.trendDays) return;
    clear(dom.trendDays);
    for (const d of trendDays) {
      const active = d.day === trendDay();
      const noData = !d.samples;
      const cls = 'trend__day' + (active ? ' is-active' : '') + (noData ? ' is-empty' : '');
      const btn = el('button', {
        class: cls,
        type: 'button',
        'aria-pressed': active ? 'true' : 'false',
        dataset: { day: d.day },
        title: noData ? '这天没有采样数据' : (d.samples + ' 条采样')
      }, [
        el('span', { class: 'trend__day-label', text: d.label }),
        el('span', { class: 'trend__day-sub', text: d.is_today && !d.complete ? '进行中' : (noData ? '无数据' : d.day.slice(5)) })
      ]);
      btn.addEventListener('click', () => {
        if (d.day === trendDay()) return;
        trendSelectedDay = d.is_today ? null : d.day;
        renderTrendDays();
        applyTrendDayCollapse();
        loadTrend(trendLastState, true);
      });
      dom.trendDays.append(btn);
    }
    applyTrendDayCollapse();
  }

  function loadTrendDays(state) {
    if (!dom.trendDays) return;
    if (USE_MOCK) {
      trendDays = mockTrendDays();
      renderTrendDays();
      return;
    }
    fetchJSON('/api/history-days?site=' + encodeURIComponent(trendSelectedSiteId) + '&days=' + TREND_DAYS)
      .then((d) => {
        trendDays = (d && d.days) || [];
        if (!trendDays.length) trendDays = mockTrendDays();
        renderTrendDays();
      })
      .catch(() => {
        trendDays = mockTrendDays();
        renderTrendDays();
      });
  }

  function setTrendMetric(metric) {
    trendMetric = metric;
    if (dom.trendMetrics) {
      dom.trendMetrics.forEach((b) => {
        const on = b.dataset.metric === metric;
        b.classList.toggle('is-active', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
    }
    const data = trendCache[trendCacheKey()];
    if (data) renderTrendChart(data);
  }

  function loadTrend(state, force) {
    if (!dom.trendChart) return;
    const key = trendCacheKey();
    if (!force && trendCache[key]) {
      renderTrendChart(trendCache[key]);
      return;
    }
    if (USE_MOCK) {
      const pts = mockTrendPoints(state, trendSelectedSiteId, trendDay());
      trendCache[key] = { points: pts, cabinets: 0, mock: true, day: trendDay() };
      renderTrendChart(trendCache[key]);
      return;
    }
    dom.trendChart.innerHTML = '<p class="trend__placeholder">正在载入历史趋势…</p>';
    const url = '/api/site-history?site=' + encodeURIComponent(trendSelectedSiteId) +
      '&day=' + encodeURIComponent(trendDay());
    fetchJSON(url)
      .then((d) => {
        trendCache[key] = { points: d.points || [], cabinets: d.cabinets || 0, day: trendDay() };
        renderTrendChart(trendCache[key]);
      })
      .catch((err) => {
        dom.trendChart.innerHTML = '';
        dom.trendChart.append(emptyState('历史趋势暂时不可用', err && err.message ? err.message : '请稍后再试。'));
      });
  }

  /* 用 SVG 画折线：墨线折线 + 安全橙当前点 + 等宽坐标标注 + 直角网格
     X 轴恒为当天 0:00–24:00（1440 分钟），数据点按「当天第几分钟」定位，
     所以只有一个上午的数据时曲线只占左边一段，不会像滚动窗口那样被拉伸满屏。 */
  function renderTrendChart(data) {
    if (!dom.trendChart) return;
    clear(dom.trendChart);
    const day = (data && data.day) || trendDay();
    const points = ((data && data.points) || []).filter((p) => {
      // 只保留当天内的点（后端已按自然日窗口裁剪，这里再兜一次防御）
      const d = new Date(p.t);
      return !Number.isNaN(d.getTime());
    });
    if (points.length < 1) {
      dom.trendChart.append(emptyState('这一天没有采样数据',
        '选「今天」或其它有数据的日子；历史从开始运行那天起累积。'));
      if (dom.trendFoot) dom.trendFoot.textContent = '';
      return;
    }

    const metric = trendMetric;
    const vals = points.map((p) => num(metric === 'free' ? p.free : p.occupied));
    let vMax = Math.max.apply(null, vals);
    let vMin = Math.min.apply(null, vals);
    const pad = Math.max(4, Math.round((vMax - vMin) * 0.18));
    vMin = Math.max(0, vMin - pad);
    vMax = vMax + pad;
    if (vMax === vMin) vMax = vMin + 1;

    // 手机用窄 viewBox：1000 宽的 viewBox 在 393px 屏上会被压成 1/3，字号小到看不清
    const compact = window.innerWidth <= 680;
    const W = compact ? 420 : 1000, H = compact ? 250 : 300;
    const L = compact ? 34 : 52, R = compact ? 12 : 16;
    const T = compact ? 18 : 22, B = compact ? 34 : 40;
    const iw = W - L - R, ih = H - T - B;

    // X 轴：0:00–24:00 → 固定 1440 分钟映射
    const DAY_MIN = 24 * 60;
    const minuteOf = (iso) => {
      const d = new Date(iso);
      return d.getHours() * 60 + d.getMinutes();
    };
    const x = (min) => L + (Math.max(0, Math.min(DAY_MIN, min)) / DAY_MIN) * iw;
    const y = (v) => T + ih - ((v - vMin) / (vMax - vMin)) * ih;

    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    if (compact) svg.setAttribute('class', 'is-compact');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', (metric === 'free' ? '可用' : '占用') + '桩位 ' + day + ' 全天趋势折线图');

    const line = (x1, y1, x2, y2, cls) => {
      const n = document.createElementNS(NS, 'line');
      n.setAttribute('x1', x1); n.setAttribute('y1', y1);
      n.setAttribute('x2', x2); n.setAttribute('y2', y2);
      if (cls) n.setAttribute('class', cls);
      svg.append(n);
      return n;
    };
    const text = (sx, sy, str, cls, anchor) => {
      const n = document.createElementNS(NS, 'text');
      n.setAttribute('x', sx); n.setAttribute('y', sy);
      if (cls) n.setAttribute('class', cls);
      if (anchor) n.setAttribute('text-anchor', anchor);
      n.textContent = str;
      svg.append(n);
      return n;
    };

    // Y 轴：4 条水平网格 + 刻度
    for (let g = 0; g <= 4; g++) {
      const gv = vMin + (vMax - vMin) * (g / 4);
      const gy = y(gv);
      line(L, gy, W - R, gy, 'trend__grid');
      text(L - 10, gy + 4, String(Math.round(gv)), 'trend__ylabel', 'end');
    }

    // X 轴：固定整点刻度（0/4/8/12/16/20/24 点），与数据多少无关
    const tickHours = compact ? [0, 6, 12, 18, 24] : [0, 3, 6, 9, 12, 15, 18, 21, 24];
    for (const h of tickHours) {
      const px = x(h * 60);
      // 每小时刻度短竖线，让「0:00–23:59 是固定轴」这件事一眼可见
      line(px, T + ih, px, T + ih + 4, 'trend__tick');
      const lab = h === 24 ? '23:59' : (String(h).padStart(2, '0') + ':00');
      text(px, H - 12, lab, 'trend__xlabel',
        h === 0 ? 'start' : (h === 24 ? 'end' : 'middle'));
    }

    // 折线路径 + 面积填充（按分钟定位，不按索引）
    // 断档处理：两桶间隔超过 20 分钟说明服务没在跑（不是数据抖动），
    // 这时把线断开（另起一个 M），否则会画出一条凭空直连的假线，
    // 让人误以为那段时间一直在采。
    const GAP_MIN = 20;
    const segs = [];          // [[i0,i1], ...] 连续段
    let segStart = 0;
    for (let i = 1; i < points.length; i++) {
      const gap = minuteOf(points[i].t) - minuteOf(points[i - 1].t);
      if (gap > GAP_MIN) {
        segs.push([segStart, i - 1]);
        segStart = i;
      }
    }
    segs.push([segStart, points.length - 1]);

    let d = '';
    const areaParts = [];
    for (const [a, b] of segs) {
      let seg = '';
      for (let i = a; i <= b; i++) {
        const px = x(minuteOf(points[i].t)).toFixed(1);
        const py = y(vals[i]).toFixed(1);
        seg += (i === a ? 'M' : ' L') + px + ' ' + py;
      }
      d += seg;
      const ax = x(minuteOf(points[a].t)).toFixed(1);
      const bx = x(minuteOf(points[b].t)).toFixed(1);
      areaParts.push(seg + ' L' + bx + ' ' + (T + ih) + ' L' + ax + ' ' + (T + ih) + ' Z');
    }
    const area = document.createElementNS(NS, 'path');
    area.setAttribute('d', areaParts.join(' '));
    area.setAttribute('class', metric === 'free' ? 'trend__area-fill' : 'trend__area-fill trend__area-fill--occ');
    svg.append(area);
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d', d);
    path.setAttribute('class', metric === 'free' ? 'trend__line' : 'trend__line trend__line--occ');
    svg.append(path);
    // 断档处画一条虚线柱，明确标出「这段时间没采到」
    for (let i = 1; i < segs.length; i++) {
      const gx = x(minuteOf(points[segs[i][0]].t)).toFixed(1);
      line(gx, T + 2, gx, T + ih, 'trend__gap');
    }

    // 数据点：点少时逐个画小点，点多（整天满采）时只在最新点画
    if (points.length <= 60) {
      for (let i = 0; i < points.length; i++) {
        const c = document.createElementNS(NS, 'circle');
        c.setAttribute('cx', x(minuteOf(points[i].t)).toFixed(1));
        c.setAttribute('cy', y(vals[i]).toFixed(1));
        c.setAttribute('r', 1.8);
        c.setAttribute('class', metric === 'free' ? 'trend__pt' : 'trend__pt trend__pt--occ');
        svg.append(c);
      }
    }

    // 最新点 + 标注
    const lastX = x(minuteOf(points[points.length - 1].t)), lastY = y(vals[vals.length - 1]);
    const dot = document.createElementNS(NS, 'circle');
    dot.setAttribute('cx', lastX); dot.setAttribute('cy', lastY);
    dot.setAttribute('r', 5);
    dot.setAttribute('class', 'trend__dot');
    svg.append(dot);
    const cur = vals[vals.length - 1];
    text(Math.min(lastX, W - R - 4), Math.max(T + 12, lastY - 12),
      String(cur) + (metric === 'free' ? ' 可用' : ' 占用'), 'trend__cur', 'end');
    dom.trendChart.append(svg);

    /* 图脚：日期 + 覆盖时段 + 均值 */
    if (dom.trendFoot) {
      const t0 = fmtClock(points[0].t).slice(0, 5);
      const t1 = fmtClock(points[points.length - 1].t).slice(0, 5);
      const metricText = metric === 'free' ? '可用' : '占用';
      const avg = Math.round(vals.reduce((a, b) => a + b, 0) / vals.length);
      const isToday = day === todayKey();
      const cover = isToday
        ? '已采集 ' + t0 + ' → ' + t1
        : '覆盖 ' + t0 + ' → ' + t1;
      dom.trendFoot.textContent = day + ' · ' + cover +
        ' · ' + metricText + '桩位均值 ' + avg +
        ' · 每 10 分钟一点 · 横轴固定 0:00–23:59';
    }
  }

  function renderSites(state) {
    const grid = dom.siteGrid;
    grid.setAttribute('aria-busy', 'false');
    clear(grid);

    const sites = (state.sites || []).slice().sort((a, b) => {
      const af = num(a.stats.ports_free_real, a.stats.ports_free), bf = num(b.stats.ports_free_real, b.stats.ports_free);
      if (bf !== af) return bf - af;
      return a.name.localeCompare(b.name, 'zh-Hans-CN');
    });

    if (!sites.length) {
      grid.append(emptyState('暂无点位数据', '接口返回的 sites 为空，可能是采集尚未完成。'));
      return;
    }

    for (const site of sites) grid.append(renderSiteCard(site));
  }

  function renderSiteCard(site) {
    const st = site.stats;
    const offline = !!site.offline;
    const card = el('article', {
      class: 'site-card',
      dataset: { level: site.level, offline: offline ? 'true' : 'false' },
      'aria-labelledby': 'site-' + site.site_id
    });

    /* --- 卡头：点位名 + 等级文字标签 --- */
    const chipText = offline ? '离线' : (LEVEL_TEXT[site.level] || '—');
    const head = el('div', { class: 'site-card__head' }, [
      el('h3', { class: 'site-card__name', id: 'site-' + site.site_id }, [
        el('span', { class: 'site-card__short', text: site.short || site.site_id }),
        document.createTextNode(site.name)
      ]),
      el('span', { class: 'lv-chip' }, [
        el('span', { class: 'lv-chip__dot', 'aria-hidden': 'true' }),
        document.createTextNode(chipText)
      ])
    ]);
    card.append(head);

    /* --- 主数字：空闲 / 总数（离线点位不显示 0/0，直接说「离线」） --- */
    const counts = offline
      ? el('div', { class: 'site-card__counts' }, [
          el('p', { class: 'count-main count-main--offline' }, [
            el('span', { class: 'count-main__num', text: '离线' })
          ]),
          el('div', { class: 'count-side' }, [
            el('span', { class: 'count-side__k', text: '在线电柜' }),
            el('span', { class: 'count-side__v', text: num(st.cabinets_online, 0) + '/' + num(st.cabinets_all, 0) + ' 台' }),
            el('span', { class: 'count-side__k', text: '桩位状态' }),
            el('span', { class: 'count-side__v', text: '无实时读数' })
          ])
        ])
      : el('div', { class: 'site-card__counts' }, [
          el('p', { class: 'count-main' }, [
            el('span', { class: 'count-main__num', text: String(num(st.ports_free_real, st.ports_free)) }),
            el('span', { class: 'count-main__slash', 'aria-hidden': 'true', text: '/' }),
            el('span', { class: 'count-main__total', text: String(st.ports_total) })
          ]),
          el('div', { class: 'count-side' }, [
            el('span', { class: 'count-side__k', text: '使用中' }),
            el('span', { class: 'count-side__v', text: st.ports_occupied + ' 个' }),
            /* 平台把「到点断电但电池没取走」当空闲，必须显式告知，否则用户白跑一趟 */
            (num(st.ports_blocked) > 0 ? el('span', { class: 'count-side__k', text: '断电未取走' }) : null),
            (num(st.ports_blocked) > 0
              ? el('span', { class: 'count-side__v count-side__v--warn', text: num(st.ports_blocked) + ' 个' })
              : null),
            el('span', { class: 'count-side__k', text: '故障桩位' }),
            el('span', { class: 'count-side__v', text: st.ports_fault + ' 个' }),
            (num(st.cabinets_offline) > 0 ? el('span', { class: 'count-side__k', text: '离线电柜' }) : null),
            (num(st.cabinets_offline) > 0 ? el('span', { class: 'count-side__v', text: num(st.cabinets_offline) + ' 台' }) : null)
          ])
        ]);
    card.append(counts);

    /* --- 空闲率条（离线点位改为斜纹占位，不画 0% 进度） --- */
    card.append(el('div', { class: 'level-bar' + (offline ? ' level-bar--offline' : ''), 'aria-hidden': 'true' }, [
      el('div', { class: 'level-bar__fill', style: 'width:' + (offline ? 100 : pct(st.free_ratio)).toFixed(1) + '%' }),
      el('i', { class: 'level-bar__tick', dataset: { at: '15' } }),
      el('i', { class: 'level-bar__tick', dataset: { at: '40' } })
    ]));

    if (offline) {
      card.append(el('p', { class: 'ports__note', text: '该点位电柜当前离线，状态未知；桩位数据已从统计中剔除，不显示最后一次读数。' }));
    }

    /* --- 按电柜类型的小条形 --- */
    const typeRows = renderTypeRows(site);
    if (typeRows) card.append(typeRows);

    /* --- 空出预测 --- */
    card.append(renderForecast(site));

    /* --- 展开：电柜表 + 桩位时间线 --- */
    card.append(renderSiteMore(site));

    return card;
  }

  function renderTypeRows(site) {
    const groups = new Map();
    for (const cab of site.cabinets || []) {
      /* 离线柜的 chargeable 是历史残值，混进类型分布会虚报空位 */
      if (isOfflineCab(cab)) continue;
      const key = cab.cabinet_type;
      if (!groups.has(key)) groups.set(key, { label: cab.type_label || TYPE_LABEL[key] || '电柜', free: 0, occ: 0, fault: 0, total: 0 });
      const g = groups.get(key);
      g.free += num(cab.chargeable);
      g.occ += num(cab.occupied);
      g.fault += num(cab.fault);
      g.total += num(cab.capacity);
    }
    if (!groups.size) return null;

    const wrap = el('div', { class: 'type-rows' });
    const ordered = Array.from(groups.entries()).sort((a, b) => a[0] - b[0]);
    for (const [, g] of ordered) {
      const total = g.total || 1;
      const track = el('div', { class: 'type-row__track', 'aria-hidden': 'true' }, [
        el('i', { class: 'type-row__free', style: 'width:' + (g.free / total * 100).toFixed(1) + '%' }),
        el('i', { class: 'type-row__occ', style: 'width:' + (g.occ / total * 100).toFixed(1) + '%' }),
        el('i', { class: 'type-row__fault', style: 'width:' + (g.fault / total * 100).toFixed(1) + '%' })
      ]);
      wrap.append(el('div', { class: 'type-row' }, [
        el('span', { class: 'type-row__k', text: g.label }),
        track,
        el('span', { class: 'type-row__v', text: g.free + '/' + g.total })
      ]));
    }
    return wrap;
  }

  function renderForecast(site) {
    const f = site.forecast;

    /* 离线点位：没有实时读数就不该给空出预测（历史样本还能算，但套在当前状态上会误导）*/
    if (site.offline) {
      return el('div', { class: 'forecast forecast--cold' }, [
        el('div', { class: 'forecast__top' }, [
          el('span', { class: 'forecast__label', text: '空出预测（推算）' }),
          el('span', { class: 'forecast__conf', text: '不可用' })
        ]),
        el('p', { class: 'forecast__cold', text: '电柜离线，没有实时空闲数，暂时无法推算空出时间。' }),
        el('p', { class: 'forecast__cold-note', text: '恢复在线后自动继续计算' })
      ]);
    }

    const cold = !f
      || f.confidence === 'none'
      || f.median_charge_minutes === null || f.median_charge_minutes === undefined
      || !(num(f.samples) > 0);

    if (cold) {
      const scopeNote = SCOPE_TEXT[site.sample_scope] || null;
      return el('div', { class: 'forecast forecast--cold' }, [
        el('div', { class: 'forecast__top' }, [
          el('span', { class: 'forecast__label', text: '空出预测（推算）' }),
          el('span', { class: 'forecast__conf', text: '数据积累中' })
        ]),
        el('p', { class: 'forecast__cold', text: '数据积累中，约需 1–2 天形成可靠估计' }),
        el('p', {
          class: 'forecast__cold-note',
          text: (f && f.samples != null ? '已采集 ' + num(f.samples) + ' 条' : '暂无完整充电记录')
            + (scopeNote ? ' · ' + scopeNote : '')
        })
      ]);
    }

    const nums = el('div', { class: 'forecast__nums' });
    const cells = [
      { v: f.expected_free_15m, k: '15 分钟内' },
      { v: f.expected_free_30m, k: '30 分钟内' },
      { v: f.expected_free_60m, k: '60 分钟内' }
    ];
    for (const cell of cells) {
      const has = cell.v !== null && cell.v !== undefined;
      const node = el('div', { class: 'forecast__n' + (has && num(cell.v) > 0 ? ' is-hot' : '') }, [
        el('b', { text: has ? String(num(cell.v)) : '—' }),
        el('span', { text: cell.k })
      ]);
      nums.append(node);
    }

    const parts = [];
    parts.push(document.createTextNode('中位充电 '));
    parts.push(el('b', { text: fmtDuration(f.median_charge_minutes) }));
    parts.push(document.createTextNode(' · 基于 ' + num(f.samples) + ' 条完整记录'));
    if (SCOPE_TEXT[site.sample_scope]) parts.push(document.createTextNode(' · ' + SCOPE_TEXT[site.sample_scope]));
    if (f.next_free_in_minutes !== null && f.next_free_in_minutes !== undefined) {
      parts.push(document.createTextNode(' · 预计 '));
      parts.push(el('b', { text: fmtDuration(f.next_free_in_minutes) }));
      parts.push(document.createTextNode(' 内出现空位'));
    }

    return el('div', { class: 'forecast' }, [
      el('div', { class: 'forecast__top' }, [
        el('span', { class: 'forecast__label', text: '空出预测（推算）' }),
        el('span', { class: 'forecast__conf', text: CONF_TEXT[f.confidence] || '' })
      ]),
      nums,
      el('p', { class: 'forecast__line' }, parts)
    ]);
  }

  function renderSiteMore(site) {
    const cabs = site.cabinets || [];
    const details = el('details', { class: 'site-more' });
    /* 展开状态跨刷新保留：applyState 每 60 秒整块重建卡片，若不记住 open，
       用户正在看的分组面板会突然折叠，看起来像"点开没反应/卡住"。 */
    const savedOpen = UI_STATE[site.site_id] && UI_STATE[site.site_id].open;
    if (savedOpen) details.open = true;
    details.addEventListener('toggle', () => {
      UI_STATE[site.site_id] = Object.assign({}, UI_STATE[site.site_id] || {}, { open: details.open });
    });
    const summary = el('summary', {}, [
      el('span', { text: '分组与桩位 · ' + cabs.length + ' 组' }),
      el('span', { class: 'site-more__icon', 'aria-hidden': 'true', text: '+' })
    ]);
    details.append(summary);
    const body = el('div', { class: 'site-more__body' });
    const groupBlock = renderGroups(site);
    if (groupBlock) body.append(groupBlock);
    else body.append(el('p', { class: 'ports__note', text: '该点位暂无可查看的分组桩位数据。' }));
    details.append(body);
    return details;
  }

  /* ------------------------------------------------------------------------
     分组桩位查看：地点 → 组（电柜 CSTFL10619）→ 组内 1–N 号桩位
     仿小程序交互：一组约 10 个桩，三态「可使用 / 被占用 / 故障」；
     点击组切换查看，点击桩位看详情（已充时长 / 剩余 / 实测遥测）。
     ------------------------------------------------------------------------ */
  /* 按真实经纬度给一个点位的电柜组排序并编号（解决"光看编号找不到是哪一组"）。
     做法：把经纬度投影到本地平面米，取点云主轴（PCA 最大特征向量）作为
     "这排桩的走向"，把每个柜投影到主轴上排序 → 第 1 组、第 2 组…；
     主轴方向用八方位中文标注（如"由西南向东北"），使"第 N 组"与现场
     从左到右/从一端到另一端的顺序一致。纯前端从同一份 lng/lat 计算，
     与地图共用数据源，天然同步；无坐标时回退原编号排序。 */
  function bearingWord(ax, ay) {
    /* ax=向东分量, ay=向北分量；返回 0..7 的八方位中文 */
    const names = ['北', '东北', '东', '东南', '南', '西南', '西', '西北'];
    const b = (Math.atan2(ax, ay) * 180 / Math.PI + 360) % 360;
    return names[Math.round(b / 45) % 8];
  }
  function geoOrderCabs(cabs) {
    const usable = cabs.filter((c) => Number.isFinite(c.lng) && Number.isFinite(c.lat));
    const all = new Map();
    usable.forEach((c, i) => all.set(c.cabinet_no, { pos: i + 1, total: usable.length, dir: '现场位置' }));
    if (usable.length < 2) return all;
    const lat0 = usable.reduce((s, c) => s + c.lat, 0) / usable.length;
    const lng0 = usable.reduce((s, c) => s + c.lng, 0) / usable.length;
    const kx = 111320 * Math.cos(lat0 * Math.PI / 180); // 经度 1° ≈ 米
    const ky = 111320;                                  // 纬度 1° ≈ 米
    const pts = usable.map((c) => ({ cab: c, x: (c.lng - lng0) * kx, y: (c.lat - lat0) * ky }));
    let sxx = 0, syy = 0, sxy = 0;
    for (const p of pts) { sxx += p.x * p.x; syy += p.y * p.y; sxy += p.x * p.y; }
    const theta = 0.5 * Math.atan2(2 * sxy, sxx - syy); // 主轴角（最大特征向量）
    let ax = Math.cos(theta), ay = Math.sin(theta);     // ax=东, ay=北
    /* 统一朝向：让"第 1 组"落在西南/南侧，阅读方向更像地图的从左到右 */
    if (Math.abs(ax) >= Math.abs(ay)) { if (ax < 0) { ax = -ax; ay = -ay; } }
    else { if (ay < 0) { ax = -ax; ay = -ay; } }
    const from = bearingWord(-ax, -ay), to = bearingWord(ax, ay);
    const dir = from === to ? '现场位置' : ('由' + from + '向' + to);
    for (const p of pts) p.t = p.x * ax + p.y * ay;
    pts.sort((a, b) => a.t - b.t);
    const m = new Map();
    pts.forEach((p, i) => m.set(p.cab.cabinet_no, { pos: i + 1, total: pts.length, dir: dir }));
    return m;
  }

  function renderGroups(site) {
    const cabs = (site.cabinets || [])
      .filter((c) => !isOfflineCab(c) && c.occupancy && Array.isArray(c.occupancy.ports) && c.occupancy.ports.length);
    /* 按地理顺序编号并排序，让标签顺序 = 现场这排桩从一端到另一端的顺序 */
    const geo = geoOrderCabs(cabs);
    cabs.sort((a, b) => (geo.get(a.cabinet_no)?.pos || 999) - (geo.get(b.cabinet_no)?.pos || 999));

    /* 离线点位无实时桩位数据；明确告知而不是画空网格 */
    if (!cabs.length) {
      const offlineCabs = (site.cabinets || []).filter(isOfflineCab);
      if (offlineCabs.length) {
        return el('div', { class: 'grp' }, [
          el('p', { class: 'ports__note', text: '该点位电柜当前离线，无法查看组内桩位实时状态。' })
        ]);
      }
      return null;
    }

    const saved = UI_STATE[site.site_id] || {};
    let activeNo = cabs.some((c) => c.cabinet_no === saved.cab) ? saved.cab : cabs[0].cabinet_no;
    const activeCab = cabs.find((c) => c.cabinet_no === activeNo);

    const wrap = el('div', { class: 'grp' });
    const firstGeo = geo.get(cabs[0].cabinet_no) || {};
    wrap.append(el('p', { class: 'grp__cap' }, [
      el('span', { text: '充电组 · 按现场位置编号' }),
      el('span', { text: (firstGeo.dir && firstGeo.dir !== '现场位置' ? firstGeo.dir + ' · ' : '') + cabs.length + ' 组' })
    ]));

    /* --- 现场位置示意条：按主轴顺序排布的小方块，标出每组序号与空闲态。
           点它等价于点组标签，让用户"从左到右第几个有空位"一眼可见。
           两端标出现场方向，帮助用户建立"第1组 = 最靠XX那端"的映射。 --- */
    const dirText = firstGeo.dir || '';
    const dirMatch = dirText.match(/^由(.+?)向(.+)$/);
    const dirFrom = dirMatch ? dirMatch[1] : '';
    const dirTo = dirMatch ? dirMatch[2] : '';
    const strip = el('div', { class: 'posstrip', role: 'group', 'aria-label': '按现场位置排列的电柜组' });
    if (dirFrom) strip.append(el('span', { class: 'posstrip__dir', text: dirFrom }));
    for (const cab of cabs) {
      const portsOfCab = cab.occupancy.ports;
      const free = portsOfCab.filter((p) => p.state === 'free').length;
      const info = geo.get(cab.cabinet_no) || {};
      const cell = el('button', {
        class: 'posstrip__cell' + (cab.cabinet_no === activeNo ? ' is-active' : ''),
        type: 'button',
        title: '第 ' + (info.pos || '?') + ' 组 · ' + cab.cabinet_no + ' · 空 ' + free + '/' + portsOfCab.length,
        dataset: { cab: cab.cabinet_no }
      }, [
        el('span', { class: 'posstrip__no', text: String(info.pos || '—') }),
        el('span', { class: 'posstrip__dot', dataset: { lv: cab.level } })
      ]);
      cell.addEventListener('click', (e) => {
        UI_STATE[site.site_id] = { cab: cab.cabinet_no, port: null };
        reRenderGroup(site.site_id, e.currentTarget.closest('.grp'));
      });
      strip.append(cell);
    }
    if (dirTo) strip.append(el('span', { class: 'posstrip__dir posstrip__dir--end', text: dirTo }));
    wrap.append(strip);

    /* --- 组标签带（可横向滚动） --- */
    const tabs = el('div', { class: 'grp__tabs', role: 'tablist' });
    for (const cab of cabs) {
      const portsOfCab = cab.occupancy.ports;
      const free = portsOfCab.filter((p) => p.state === 'free').length;
      const blockedN = portsOfCab.filter((p) => p.state === 'blocked').length;
      const info = geo.get(cab.cabinet_no) || {};
      const tab = el('button', {
        class: 'grp__tab' + (cab.cabinet_no === activeNo ? ' is-active' : ''),
        type: 'button',
        role: 'tab',
        dataset: { cab: cab.cabinet_no, lv: cab.level },
        'aria-selected': cab.cabinet_no === activeNo ? 'true' : 'false'
      }, [
        el('span', { class: 'grp__tab-pos', text: '第' + (info.pos || '?') + '组' }),
        el('b', { text: cab.cabinet_no }),
        el('span', {
          class: 'grp__tab-free',
          dataset: { lv: cab.level },
          text: '空 ' + free + '/' + portsOfCab.length
        }),
        /* 断电未取走的桩位在组标签上就提示，不用点进去才知道 */
        (blockedN > 0 ? el('span', { class: 'grp__tab-blocked', text: '断电 ' + blockedN }) : null)
      ]);
      tab.addEventListener('click', (e) => {
        UI_STATE[site.site_id] = { cab: cab.cabinet_no, port: null };
        /* 关键：从事件目标向上找当前 DOM 中真实的 .grp。
           不能捕获 renderGroups 里的 wrap——重绘后新 tab 捕获的会是
           从未插入 DOM 的临时节点，导致第二次点击“没反应”。 */
        const currentWrap = e.currentTarget.closest('.grp');
        reRenderGroup(site.site_id, currentWrap);
      });
      tabs.append(tab);
    }
    wrap.append(tabs);
    wrap.append(renderGroupBody(site, activeCab, geo.get(activeCab.cabinet_no)));
    return wrap;
  }

  /* 把激活的组标签/位置格滚到横向条的可视区中心。
     12 组时后面的组（第 10~12 组）默认在滚动区外，切过去必须主动滚，
     否则用户点了地图气泡/位置条后看不到当前组落在哪一格。只改容器
     scrollLeft，不碰页面纵向滚动，避免把用户从当前阅读位置带走。 */
  function scrollActiveIntoView(wrap) {
    if (!wrap) return;
    ['.posstrip', '.grp__tabs'].forEach((sel) => {
      const bar = wrap.querySelector(sel);
      if (!bar) return;
      const active = bar.querySelector('.is-active');
      if (!active) return;
      const target = active.offsetLeft - bar.clientWidth / 2 + active.offsetWidth / 2;
      const max = bar.scrollWidth - bar.clientWidth;
      bar.scrollLeft = Math.max(0, Math.min(max, target));
    });
  }

  /* 仅重绘分组区（不整页重拉），保留用户点选 */
  function reRenderGroup(siteId, wrap) {
    const payload = runtime.lastPayload;
    const site = payload && (payload.sites || []).find((s) => s.site_id === siteId);
    if (!site) return;
    /* 关键：wrap 可能已经被 60 秒自动刷新 / 手动刷新的 renderSites 整块重建
       从 DOM 中移除。往一个脱离 DOM 的旧节点重绘，用户看到的就是"点了没反应、
       卡住切不了组"。isConnected 为假时，从当前 document 重新定位该点位的真实
       .grp，保证重绘落在屏幕上正在显示的节点上。 */
    let live = wrap && wrap.isConnected ? wrap : null;
    if (!live) {
      const card = document.querySelector('.site-card[aria-labelledby="site-' + siteId + '"]');
      live = card && card.querySelector('.grp');
    }
    if (!live) return;
    const fresh = renderGroups(site);
    if (!fresh) return;
    clear(live);
    for (const child of Array.from(fresh.childNodes)) live.append(child);
    scrollActiveIntoView(live);
  }

  function renderGroupBody(site, cab, geoInfo) {
    const body = el('div', { class: 'grp__body' });
    const ports = cab.occupancy.ports.slice().sort((a, b) => num(a.port) - num(b.port));
    const saved = UI_STATE[site.site_id] || {};
    let activePort = ports.find((p) => p.port === saved.port) || null;

    const counts = {
      free: ports.filter((p) => p.state === 'free').length,
      occupied: ports.filter((p) => p.state === 'occupied').length,
      fault: ports.filter((p) => p.state === 'fault').length
    };

    body.append(el('div', { class: 'grp__head' }, [
      el('div', {}, [
        (geoInfo && geoInfo.pos
          ? el('span', { class: 'grp__pos', text: '第 ' + geoInfo.pos + ' 组' })
          : null),
        el('b', { class: 'grp__no', text: cab.cabinet_no }),
        el('span', { class: 'grp__type', text: cab.type_label || '充电桩' })
      ]),
      el('div', { class: 'grp__legend' }, [
        el('span', { class: 'lg lg--free', text: '可使用 ' + counts.free }),
        el('span', { class: 'lg lg--occupied', text: '被占用 ' + counts.occupied }),
        el('span', { class: 'lg lg--fault', text: '故障 ' + counts.fault })
      ])
    ]));

    /* --- 桩位网格：仿小程序 1–10 号方块 --- */
    const stateLabel = (s) => s === 'free' ? '可使用'
      : s === 'occupied' ? '被占用'
      : s === 'blocked' ? '已断电' : '故障';
    const grid = el('div', { class: 'portgrid' });
    for (const p of ports) {
      const isReal = p.source === 'platform_real';
      const btn = el('button', {
        class: 'portcell portcell--' + p.state + (activePort && activePort.port === p.port ? ' is-active' : ''),
        type: 'button',
        dataset: { port: p.port, state: p.state },
        'aria-label': p.port + ' 号桩 ' + stateLabel(p.state)
      }, [
        el('b', { text: String(p.port) }),
        el('span', { text: stateLabel(p.state) }),
        isReal && (p.state === 'occupied' || p.state === 'blocked')
          ? el('i', { class: 'portcell__dot', 'aria-hidden': 'true' }) : null
      ]);
      btn.addEventListener('click', () => {
        UI_STATE[site.site_id] = { cab: cab.cabinet_no, port: p.port };
        /* 只切换选中态，不重新拉数据 */
        const cells = body.querySelectorAll('.portcell');
        cells.forEach((c) => c.classList.toggle('is-active', c.dataset.port === String(p.port)));
        const detailNode = renderPortDetail(p);
        const oldDetail = body.querySelector('.portdetail');
        if (oldDetail) oldDetail.replaceWith(detailNode);
        else {
          const hint = body.querySelector('.grp__hint');
          if (hint) hint.replaceWith(detailNode);
          else body.append(detailNode);
        }
      });
      grid.append(btn);
    }
    body.append(grid);

    if (activePort) body.append(renderPortDetail(activePort));
    else body.append(el('p', { class: 'grp__hint', text: '点选上方桩位，查看充电时长与实时状态。' }));

    /* --- 数据来源说明 --- */
    const real = cab.occupancy.source === 'platform_real';
    const notes = [];
    if (real) {
      const exactN = ports.filter((p) => p.remaining_is_exact).length;
      notes.push('本组逐桩位状态、已充时长、电量来自平台真实数据。');
      if (exactN > 0) {
        notes.push('其中 ' + exactN + ' 个桩位的目标时长已确定，剩余与结束时刻为精确值；其余为推断。');
      } else {
        notes.push('「剩余」为按档位分布推断，实际可能因用户手动结束而提前。');
      }
    } else {
      notes.push('本组尚未接入逐桩位真实数据，桩位编号与时长按空闲数变化推定。');
    }
    body.append(el('p', { class: 'ports__note', text: notes.join(' ') }));
    return body;
  }

  /* 单个桩位详情：占用显示已充 / 目标 / 剩余 / 结束时刻 / 电量电压电流 */
  function renderPortDetail(p) {
    const wrap = el('div', { class: 'portdetail' });
    const isReal = p.source === 'platform_real';
    const stateText = p.state === 'free' ? '可使用'
      : p.state === 'occupied' ? '被占用'
      : p.state === 'blocked' ? '已断电未取走' : '故障停用';
    wrap.append(el('div', { class: 'portdetail__head' }, [
      el('b', { text: '#' + p.port + ' 号桩' }),
      el('span', { class: 'portdetail__state', dataset: { state: p.state }, text: stateText }),
      isReal ? el('span', { class: 'port__tag port__tag--real', text: '实测' }) : null
    ]));

    if (p.state === 'occupied' || p.state === 'blocked') {
      const elapsed = p.elapsed_minutes === null || p.elapsed_minutes === undefined ? null : num(p.elapsed_minutes);
      const remain = p.remaining_minutes === null || p.remaining_minutes === undefined ? null : Math.max(0, num(p.remaining_minutes));
      const target = p.target_minutes === null || p.target_minutes === undefined ? null : num(p.target_minutes);
      const rows = [['已充时长', (p.elapsed_is_lower_bound ? '至少 ' : '') + fmtDuration(elapsed)]];

      if (target !== null) {
        // 目标时长已知：给出精确的剩余与结束时刻
        let label = '目标时长';
        if (!p.remaining_is_exact && p.target_probability !== null) {
          label = '目标时长（推断 ' + Math.round(num(p.target_probability) * 100) + '%）';
        }
        rows.push([label, fmtDuration(target)]);
      }
      rows.push([
        '预计剩余',
        remain === null ? '—'
          : (remain <= 0 ? '即将空出'
            : (p.remaining_is_upper_bound && !p.remaining_is_exact ? '至多 ' : '') + fmtRemaining(remain))
      ]);
      if (p.end_at) rows.push(['预计结束', fmtClock(p.end_at)]);

      if (isReal) {
        if (p.charger_status_text) rows.push(['充电状态', p.charger_status_text]);
        if (p.firmware_status_text && p.firmware_status_text !== p.charger_status_text) {
          rows.push(['桩端状态', p.firmware_status_text]);
        }
        if (p.percentage !== null && p.percentage !== undefined && p.percentage !== '') rows.push(['电量', p.percentage + '%']);
        if (p.voltage !== null && p.voltage !== undefined) rows.push(['电压', num(p.voltage).toFixed(1) + ' V']);
        if (p.ampere !== null && p.ampere !== undefined) rows.push(['电流', num(p.ampere).toFixed(3) + ' A']);
        if (p.energy_kwh !== null && p.energy_kwh !== undefined) rows.push(['已充电量', num(p.energy_kwh).toFixed(2) + ' kWh']);
        if (p.start_time) rows.push(['开始时间', String(p.start_time).slice(11, 19)]);
        if (p.cabox_id) rows.push(['物理桩号', String(p.cabox_id)]);
      }
      const kv = el('div', { class: 'portdetail__grid' });
      for (const [k, v] of rows) {
        kv.append(el('div', { class: 'portdetail__cell' }, [
          el('span', { class: 'k', text: k }),
          el('b', { text: v })
        ]));
      }
      wrap.append(kv);

      const tags = [];
      if (p.remaining_is_exact) {
        tags.push('该桩位目标时长已确定，结束时刻为精确值。');
      } else if (p.estimate_basis === 'slot_constraint') {
        tags.push('已充时长已达最长档位，剩余为精确值。');
      } else if (p.target_minutes) {
        tags.push('目标时长由「已充时长 + 平台档位」推断，实际结束时刻可能提前（用户手动结束）。');
      } else if (p.estimate_basis === 'exponential_prior') {
        tags.push('剩余时长按 ' + PRIOR_MINUTES + ' 分钟中位数衰减推算。');
      }
      if (p.elapsed_is_lower_bound) tags.push('「至少已充」为服务器首次看到占用后的时长，非真实插枪时间。');
      if (p.state === 'blocked') tags.push('桩已到点断电，但电池仍插在柜里，实际无法使用。');
      if (tags.length) wrap.append(el('p', { class: 'ports__note', text: tags.join(' ') }));
    } else {
      wrap.append(el('p', {
        class: 'portdetail__idle',
        text: p.state === 'free' ? '该桩位当前空闲，可直接使用。' : '该桩位故障停用，无法充电。'
      }));
    }
    return wrap;
  }

  function emptyState(kicker, text) {
    return el('div', { class: 'empty-state' }, [
      el('p', { class: 'empty-state__k', text: kicker }),
      el('p', { class: 'empty-state__v', text: text })
    ]);
  }

  /* ------------------------------------------------------------------------
     8. 状态条 / 倒计时 / 刷新循环
     ------------------------------------------------------------------------ */
  const runtime = {
    remaining: REFRESH_SECONDS,
    total: REFRESH_SECONDS,
    inflight: false,
    receivedAt: 0,
    serverAge: 0,
    timer: null,
    lastPayload: null
  };

  function setBadge(mode, text) {
    if (dom.badge) dom.badge.dataset.mode = mode;
    if (dom.badgeText) dom.badgeText.textContent = text;
  }

  function setNote(text, tone) {
    if (!dom.note) return;
    dom.note.textContent = text || '';
    if (tone) dom.note.dataset.tone = tone;
    else dom.note.removeAttribute('data-tone');
  }

  function setBusy(busy) {
    if (!dom.btnRefresh) return;
    dom.btnRefresh.setAttribute('aria-busy', busy ? 'true' : 'false');
    if (dom.btnRefreshText) dom.btnRefreshText.textContent = busy ? '刷新中…' : '立即刷新';
  }

  function paintCountdown() {
    if (dom.countdown) dom.countdown.textContent = String(Math.max(0, Math.ceil(runtime.remaining)));
    if (dom.track) {
      const progress = runtime.total > 0 ? 1 - Math.max(0, runtime.remaining) / runtime.total : 1;
      dom.track.style.transform = 'scaleX(' + Math.max(0, Math.min(1, progress)).toFixed(3) + ')';
    }
  }

  function paintAge() {
    if (!dom.factAge) return;
    const live = Math.max(0, runtime.serverAge + (Date.now() - runtime.receivedAt) / 1000);
    if (!runtime.receivedAt) { dom.factAge.textContent = '—'; return; }
    if (live < 90) dom.factAge.textContent = Math.round(live) + ' 秒前';
    else dom.factAge.textContent = Math.round(live / 60) + ' 分钟前';
  }

  function tick() {
    runtime.remaining -= 1;
    paintCountdown();
    paintAge();
    if (runtime.remaining <= 0) {
      if (!runtime.inflight) load(false);
      else runtime.remaining = 1;
    }
  }

  function startClock() {
    if (runtime.timer) clearInterval(runtime.timer);
    runtime.timer = setInterval(tick, 1000);
  }

  /* 登录凭证面板状态：只读展示 /api/state 的 auth 对象，不保存 token 到任何浏览器存储 */
  function renderAuth(auth) {
    if (!dom.authState) return;
    const a = auth || { mode: 'public', token_configured: false, valid: false };
    if (USE_MOCK) {
      dom.authState.textContent = '演示模式（?mock）：凭证面板不会向后端保存任何内容。';
      dom.authState.removeAttribute('data-tone');
      return;
    }
    if (!a.token_configured) {
      dom.authState.textContent = a.reason || '当前未配置 token，逐桩位状态为推算值。';
      dom.authState.removeAttribute('data-tone');
    } else if (a.valid) {
      const ports = num(a.real_ports);
      dom.authState.textContent = '凭证有效' +
        (a.mobile_masked ? ' · 手机号 ' + a.mobile_masked : '') +
        ' · 已接入 ' + num(a.detail_cabinets) + ' 台电柜的逐桩位真实数据（共 ' + ports + ' 个桩位）。' +
        (a.detail_error ? ' 详情：' + a.detail_error : '');
      dom.authState.dataset.tone = 'ok';
    } else {
      dom.authState.textContent = '凭证无效或未验证：' + (a.reason || '未知原因') +
        '。页面继续使用公开推算数据；请检查 token 后重新验证。';
      dom.authState.dataset.tone = 'warn';
    }
  }

  /* 保存 / 验证 / 清除 token：只走 /api/config 与 /api/auth/check */
  function authRequest(url, body, btn, successText) {
    if (USE_MOCK) {
      if (dom.authState) {
        dom.authState.textContent = '演示模式下不可修改凭证。';
        dom.authState.removeAttribute('data-tone');
      }
      return Promise.resolve();
    }
    if (btn) btn.setAttribute('aria-busy', 'true');
    if (dom.authState) {
      dom.authState.textContent = '正在处理…';
      dom.authState.removeAttribute('data-tone');
    }
    return fetchJSON(url, {
      method: 'POST',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
      .then((res) => {
        /* /api/config 保存新 token 时 auth_check 是即时验证结果，优先于可能滞后的 auth */
        const auth = (res && res.auth_check) || (res && res.auth) || res || {};
        renderAuth(auth);
        if (dom.authState && successText && auth.valid) {
          dom.authState.textContent = successText(auth);
          dom.authState.dataset.tone = 'ok';
        }
        /* 凭证变化后立刻重拉一次状态，让桩位卡片切到实测/推算 */
        return load(false);
      })
      .catch((err) => {
        if (dom.authState) {
          dom.authState.textContent = '操作失败：' + (err && err.message ? err.message : '未知错误');
          dom.authState.dataset.tone = 'warn';
        }
      })
      .finally(() => { if (btn) btn.removeAttribute('aria-busy'); });
  }

  function applyState(state) {
    runtime.lastPayload = state;
    runtime.receivedAt = Date.now();

    /* 用服务端时间差估算采集距今，不依赖本机时钟 */
    let age = 0;
    const fresh = state.freshness;
    if (fresh && fresh.last_poll_at && state.server_time) {
      const pollMs = Date.parse(fresh.last_poll_at);
      const serverMs = Date.parse(state.server_time);
      if (Number.isFinite(pollMs) && Number.isFinite(serverMs)) age = Math.max(0, (serverMs - pollMs) / 1000);
    }
    runtime.serverAge = age;

    if (dom.factPoll) dom.factPoll.textContent = fresh && fresh.last_poll_at ? fmtClock(fresh.last_poll_at) : '—';

    renderAuth(state.auth);
    renderOverview(state);
    renderInsights(state);
    renderCompare(state);
    renderFinder(state);
    renderSites(state);
    trendLastState = state;
    renderTrendAreas(state);
    loadTrendDays(state);
    loadTrend(state, false);
    renderMap(state);

    const stale = !!(fresh && fresh.stale) || age > 150;
    if (state.mock) setBadge('mock', 'MOCK 演示数据');
    else if (state.mode === 'auth') setBadge('live', '已登录 · 实时');
    else setBadge('live', '公开接口 · 实时');

    if (stale) {
      setBadge('stale', '数据陈旧');
      setNote('最后一次成功采集已超过 2 分半，正在重试；页面显示的是旧数据。', 'warn');
    } else if (state.mock) {
      setNote('当前为内置演示数据（?mock），未连接后端接口。', 'ok');
    } else {
      setNote('数据正常 · 下次自动刷新倒计时见上方', 'ok');
    }

    if (dom.siteGrid) dom.siteGrid.setAttribute('aria-busy', 'false');
  }

  function applyError(err) {
    if (dom.siteGrid) dom.siteGrid.setAttribute('aria-busy', 'false');
    setBadge('error', '连接失败');
    setNote('拉取 /api/state 失败：' + (err && err.message ? err.message : '未知错误') + '，' + RETRY_SECONDS + ' 秒后重试。', 'warn');
    if (!runtime.lastPayload) {
      clear(dom.statRow);
      for (let i = 0; i < 5; i++) dom.statRow.append(el('div', { class: 'stat stat--skeleton' }));
      clear(dom.compareList);
      clear(dom.insights);
      clear(dom.siteGrid);
      dom.siteGrid.append(emptyState('数据暂时不可用', '接口没有返回数据。请确认后端已启动，或访问 ?mock 查看演示数据。'));
    }
  }

  function load(manual) {
    if (runtime.inflight) return Promise.resolve();
    runtime.inflight = true;
    if (manual) setBusy(true);

    const begin = Date.now();
    const request = (USE_MOCK
      ? new Promise((resolve, reject) => {
          if (FLAGS.fail) { reject(new Error('mock: 模拟接口失败')); return; }
          setTimeout(() => resolve(buildMockState()), 120);
        })
      : fetchJSON('/api/state')
    ).then(normalizeState);

    return request
      .then((state) => {
        applyState(state);
        runtime.total = Math.max(5, num((state.freshness || {}).interval_seconds, REFRESH_SECONDS));
        runtime.remaining = runtime.total;
      })
      .catch((err) => {
        applyError(err);
        runtime.total = RETRY_SECONDS;
        runtime.remaining = RETRY_SECONDS;
      })
      .finally(() => {
        runtime.inflight = false;
        if (manual) setTimeout(() => setBusy(false), Math.max(0, 400 - (Date.now() - begin)));
        paintCountdown();
        startClock();
      });
  }

  /* 手动刷新：先请后端立刻向上游拉一次，再读状态 */
  function manualRefresh() {
    if (runtime.inflight) return;
    if (USE_MOCK) { load(true); return; }
    setBusy(true);
    fetchJSON('/api/poll', { method: 'POST' })
      .catch(() => null) /* 限频或失败都不阻塞：直接读现有状态 */
      .then(() => new Promise((r) => setTimeout(r, POLL_SETTLE_MS)))
      .then(() => load(true))
      .catch(() => load(true));
  }

  /* 从整条抓包文本里识别 token + mobile。
     支持 Fiddler / Charles / 浏览器 DevTools 复制出来的任意格式：
       - header: "token: 96位十六进制"
       - body:   "mobile=13800138000"（urlencoded 或 JSON）
     只在本机正则提取，不发送任何网络请求。 */
  function parseCaptureText(text) {
    const src = String(text || '');
    let token = '';
    let mobile = '';

    /* 1) token：请求头 token: <值>，兼容带空格/冒号/引号；值多为 96 位十六进制 */
    const headerRe = /(?:^|\n)\s*token\s*[:：]\s*["']?([0-9a-fA-F]{24,})["']?\s*(?:\r?\n|$)/i;
    const mHeader = src.match(headerRe);
    if (mHeader) token = mHeader[1];
    if (!token) {
      /* 兜底：任意位置出现的一长串 32~128 位十六进制 */
      const mAny = src.match(/\b([0-9a-f]{32,128})\b/i);
      if (mAny) token = mAny[1];
    }

    /* 2) mobile：mobile=<11位> 或 "mobile":"<11位>" 或 "mobile": <11位> */
    const mobileRe = /(?:mobile["']?\s*[:=]\s*["']?|mobile=)(1[3-9]\d{9})\b/i;
    const mMobile = src.match(mobileRe);
    if (mMobile) mobile = mMobile[1];
    if (!mobile) {
      const mBare = src.match(/\b(1[3-9]\d{9})\b/);
      if (mBare) mobile = mBare[1];
    }

    return { token: token, mobile: mobile };
  }

  function handleParsePaste() {
    const raw = (dom.authPaste && dom.authPaste.value || '').trim();
    const hint = dom.authPasteHint;
    if (!raw) {
      if (hint) { hint.textContent = '请先粘贴抓包内容再点识别。'; hint.dataset.tone = 'warn'; }
      return;
    }
    const got = parseCaptureText(raw);
    if (got.token && dom.authToken) dom.authToken.value = got.token;
    if (got.mobile && dom.authMobile) dom.authMobile.value = got.mobile;

    if (got.token) {
      const parts = ['已识别 token（' + got.token.length + ' 位）'];
      if (got.mobile) parts.push('手机号 ' + got.mobile.replace(/(\d{3})\d{4}(\d{4})/, '$1****$2'));
      else parts.push('未识别到手机号，请在下方手动补填');
      parts.push('现在点「保存并验证」即可');
      if (hint) { hint.textContent = parts.join(' · '); hint.dataset.tone = 'ok'; }
      if (dom.authState) {
        dom.authState.textContent = '已自动填入凭证，请核对后点「保存并验证」。';
        dom.authState.removeAttribute('data-tone');
      }
    } else {
      if (hint) {
        hint.textContent = '未识别到 token。请确认粘贴的是 cloud.tosunelectric.com 的完整请求（含 token 请求头），或直接手动填写下方字段。';
        hint.dataset.tone = 'warn';
      }
    }
  }

  /* ------------------------------------------------------------------------
     9. 启动
     ------------------------------------------------------------------------ */
  function boot() {
    if (dom.btnRefresh) dom.btnRefresh.addEventListener('click', manualRefresh);
    if (dom.authParse) dom.authParse.addEventListener('click', handleParsePaste);
    if (dom.finderToggle) dom.finderToggle.addEventListener('click', () => {
      finderExpanded = !finderExpanded;
      applyFinderCollapse();
    });
    applyFinderCollapse();

    if (dom.trendMetrics) dom.trendMetrics.forEach((b) => {
      b.addEventListener('click', () => setTrendMetric(b.dataset.metric));
    });

    if (dom.trendDaytoggle) dom.trendDaytoggle.addEventListener('click', () => {
      trendDaysExpanded = !trendDaysExpanded;
      applyTrendDayCollapse();
    });
    applyTrendDayCollapse();

    if (dom.authSave) dom.authSave.addEventListener('click', () => {
      const token = (dom.authToken && dom.authToken.value || '').trim();
      const mobile = (dom.authMobile && dom.authMobile.value || '').trim();
      if (!token) {
        if (dom.authState) {
          dom.authState.textContent = '请先粘贴 token，或使用「清除」按钮移除已保存的凭证。';
          dom.authState.dataset.tone = 'warn';
        }
        return;
      }
      const body = { token: token };
      if (mobile) body.mobile = mobile;
      authRequest('/api/config', body, dom.authSave, (a) =>
        '凭证有效' + (a.mobile_masked ? ' · 手机号 ' + a.mobile_masked : '') +
        ' · 后台正在拉取逐桩位真实数据，稍后自动切换。');
    });

    if (dom.authCheck) dom.authCheck.addEventListener('click', () => {
      authRequest('/api/auth/check', { force: true }, dom.authCheck, null);
    });

    if (dom.authClear) dom.authClear.addEventListener('click', () => {
      if (dom.authToken) dom.authToken.value = '';
      if (dom.authMobile) dom.authMobile.value = '';
      authRequest('/api/config', { token: '', mobile: '' }, dom.authClear, null);
    });

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && runtime.remaining <= 0 && !runtime.inflight) load(false);
    });

    paintCountdown();
    if (dom.factCountdown) dom.factCountdown.textContent = String(REFRESH_SECONDS);
    load(false);

    /* 说明：COUNTDOWN 由 startClock 每秒推进；标签页隐藏时浏览器会降频，
       回到前台时若已过点则立即补一次拉取。 */
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
