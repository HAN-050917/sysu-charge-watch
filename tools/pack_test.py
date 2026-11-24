"""异机模拟：解压 zip 到临时目录，用包内 exe 独立启动，验证趋势 API 与前端资源齐全。"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZIP = ROOT / "dist_包" / "中大深圳充电桩看板-一键启动包.zip"
PORT = 8797

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path, timeout=25):
    with op.open(f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as r:
        return r.status, r.read()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="charge_pack_"))
    print("临时目录:", tmp)
    shutil.unpack_archive(str(ZIP), str(tmp))
    pkg = tmp / "中大深圳充电桩看板-一键启动包"
    exe = pkg / "SYSUChargeWatch.exe"
    if not exe.exists():
        print("FAIL 找不到 exe")
        return 1

    proc = subprocess.Popen(
        [str(exe), "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(pkg),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    fails = []
    try:
        # 等服务起来
        up = False
        for _ in range(40):
            time.sleep(1.5)
            try:
                s, _b = get("/api/health", timeout=5)
                if s == 200:
                    up = True
                    break
            except Exception:
                pass
        if not up:
            print("FAIL 服务未启动")
            return 1
        print("服务已启动")

        # 1. health
        s, b = get("/api/health")
        h = json.loads(b)
        print("health:", s, "ok=" + str(h.get("ok")), "cabinets=" + str(h.get("cabinets")))
        if not h.get("ok"):
            fails.append("health.ok false")

        # 2. 首页 + 趋势区块
        s, b = get("/")
        html = b.decode("utf-8", "replace")
        print("/ 长度", len(html))
        for k in ('id="trend"', 'id="trend-chart"', 'id="trend-areas"',
                  'data-metric="occupied"', 'id="trend-daybar"', 'id="trend-days"',
                  'id="trend-daytoggle"', 'id="trend-daycur"'):
            if k not in html:
                fails.append("index.html 缺 " + k)
        s, b = get("/app.js")
        js = b.decode("utf-8", "replace")
        for k in ("renderTrendChart", "loadTrend", "setTrendMetric", "is-compact",
                  "loadTrendDays", "renderTrendDays", "applyTrendDayCollapse",
                  "trendCacheKey", "todayKey"):
            if k not in js:
                fails.append("app.js 缺 " + k)
        s, b = get("/styles.css")
        css = b.decode("utf-8", "replace")
        for k in (".trend__chart", ".trend__metric", ".trend__line", "svg.is-compact",
                  ".trend__daybar", ".trend__daytoggle", ".trend__day"):
            if k not in css:
                fails.append("styles.css 缺 " + k)

        # 3. 等首轮采集后验证趋势 API
        print("等首轮采集…")
        seen = False
        for i in range(24):
            time.sleep(2)
            try:
                s, b = get("/api/site-history?site=all&hours=24", timeout=20)
                d = json.loads(b)
                pts = d.get("points") or []
                if pts:
                    seen = True
                    print(f"site-history: status={s} count={d.get('count')} "
                          f"cabinets={d.get('cabinets')} points={len(pts)}")
                    print("  first:", pts[0])
                    print("  last :", pts[-1])
                    for k in ("t", "free", "occupied", "cabinets"):
                        if k not in pts[0]:
                            fails.append("point 缺字段 " + k)
                    if d.get("cabinets", 0) < 10:
                        fails.append(f"参与聚合电柜过少 {d.get('cabinets')}")
                    break
            except Exception as e:
                if i > 20:
                    fails.append("site-history 异常 " + str(e)[:80])
        if not seen:
            fails.append("site-history 24 秒内未返回数据点")

        # 4. 具体点位
        for sid in ("dongjiaoxuelou", "dongyuansushe"):
            try:
                s, b = get(f"/api/site-history?site={sid}&hours=24", timeout=20)
                d = json.loads(b)
                print(f"site-history {sid}: count={d.get('count')} cabinets={d.get('cabinets')}")
            except Exception as e:
                fails.append(f"{sid} 异常 " + str(e)[:60])

        # 5. 非法 hours 应被 clamp 而不是 500
        s, b = get("/api/site-history?site=all&hours=99999", timeout=20)
        d = json.loads(b)
        print("hours clamp:", d.get("hours"))
        if d.get("hours") != 720:
            fails.append("hours 未 clamp 到 720，实际 " + str(d.get("hours")))

        # 6. 自然日窗口：给了 day 就必须有 window，且边界是当天 00:00 → 次日 00:00
        today = time.strftime("%Y-%m-%d")
        s, b = get("/api/site-history?site=all&day=" + today, timeout=25)
        d = json.loads(b)
        print(f"day={today}: status={s} count={d.get('count')} window={d.get('window')}")
        if s != 200:
            fails.append("day 请求非 200")
        w = d.get("window") or {}
        if not w.get("start", "").startswith(today + "T00:00"):
            fails.append("window.start 不是当天 00:00：" + str(w.get("start")))
        if not d.get("day") == today:
            fails.append("响应未回显 day：" + str(d.get("day")))
        pts = d.get("points") or []
        if pts:
            # 所有点都必须落在当天
            bad = [p for p in pts if not str(p.get("t", "")).startswith(today)]
            if bad:
                fails.append(f"有 {len(bad)} 个点不在当天，首个 {bad[0].get('t')}")
            # 每个整点刻度都能被 axis 定位：首点应是 00:00 桶（当天有完整采样时）
            print("  day first:", pts[0]["t"], "last:", pts[-1]["t"])
        else:
            fails.append("day 查询无数据点")

        # 7. 非法日期应 400 而不是 500
        try:
            s, b = get("/api/site-history?site=all&day=bad-date", timeout=15)
            if s != 400:
                fails.append(f"非法 day 应为 400，实际 {s}")
            else:
                print("bad day ->", json.loads(b))
        except urllib.error.HTTPError as e:
            if e.code != 400:
                fails.append(f"非法 day 应为 400，实际 {e.code}")
            else:
                print("bad day ->", json.loads(e.read().decode("utf-8")))

        # 8. 日期栏接口：7 天、按倒序、今天 complete=false
        s, b = get("/api/history-days?site=all&days=7", timeout=20)
        dd = json.loads(b)
        days = dd.get("days") or []
        print("history-days:", [(x.get("label"), x.get("samples")) for x in days])
        if len(days) != 7:
            fails.append(f"history-days 应 7 天，实际 {len(days)}")
        else:
            if not days[0].get("is_today"):
                fails.append("history-days[0] 不是今天")
            if days[0].get("complete") is not False:
                fails.append("今天的 complete 应为 false")
            if days[1].get("complete") is not True:
                fails.append("昨天的 complete 应为 true")
            if not days[0].get("day", "") > days[-1].get("day", ""):
                fails.append("history-days 未按时间倒序")
            if (days[0].get("samples") or 0) <= 0:
                fails.append("今天应有采样行数")
        # days clamp
        s, b = get("/api/history-days?site=all&days=999", timeout=20)
        dd2 = json.loads(b)
        if len(dd2.get("days") or []) != 31:
            fails.append(f"days 未 clamp 到 31，实际 {len(dd2.get('days') or [])}")
        else:
            print("days clamp: 31 OK")
        # 具体点位的日期栏
        s, b = get("/api/history-days?site=dongjiaoxuelou&days=7", timeout=20)
        ddz = json.loads(b)
        print("history-days 东教:", [(x.get("label"), x.get("samples")) for x in ddz.get("days", [])])
        if (ddz.get("days") or [{}])[0].get("samples", 0) <= 0:
            fails.append("东教今天应有采样")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        time.sleep(1)
        shutil.rmtree(tmp, ignore_errors=True)
        print("已清理临时目录")

    print()
    if fails:
        print("=== FAIL (" + str(len(fails)) + ") ===")
        for f in fails:
            print("  ✗ " + f)
        return 1
    print("PACK EXE TEST: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
