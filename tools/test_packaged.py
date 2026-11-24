# 实测打包后的 exe：从中文路径启动 → 请求 API → 关闭
import os, subprocess, sys, time, urllib.request
from pathlib import Path

# 本脚本位于 tools/ 下，包目录在项目根的 dist_包/ 下
ROOT = Path(__file__).resolve().parent.parent
PKG = str(ROOT / "dist_包" / "中大深圳充电桩看板-一键启动包")
EXE = os.path.join(PKG, "SYSUChargeWatch.exe")
PORT = 8891
BASE = "http://127.0.0.1:%d" % PORT

# 用独立进程组启动，避免被父进程结束误杀
CREATE_NEW_PROCESS_GROUP = 0x00000200
proc = subprocess.Popen(
    [EXE, "--host", "127.0.0.1", "--port", str(PORT), "--no-poll"],
    cwd=PKG, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace",
    creationflags=CREATE_NEW_PROCESS_GROUP,
)

def no_proxy():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))

try:
    # 等待端口起来
    ok = False
    for i in range(60):
        if proc.poll() is not None:
            out = proc.stdout.read()
            print("EXE 提前退出，输出:\n", out[:3000])
            sys.exit(1)
        try:
            r = no_proxy().open(BASE + "/api/state", timeout=2)
            if r.status == 200:
                ok = True
                break
        except Exception:
            time.sleep(0.5)
    if not ok:
        print("超时未起来")
        proc.terminate()
        sys.exit(1)

    print("=== API 测试 ===")
    st = no_proxy().open(BASE + "/api/state", timeout=5)
    import json
    data = json.loads(st.read().decode("utf-8"))
    sites = data.get("sites", [])
    print("/api/state HTTP", st.status, "| 点位数", len(sites))
    total_cab = sum(len(s.get("cabinets", [])) for s in sites)
    print("电柜总数", total_cab)

    st2 = no_proxy().open(BASE + "/api/meta", timeout=5)
    meta = json.loads(st2.read().decode("utf-8"))
    print("/api/meta HTTP", st2.status, "| campus", bool(meta.get("campus")))

    st3 = no_proxy().open(BASE + "/", timeout=5)
    html = st3.read().decode("utf-8", "replace")
    print("/ HTTP", st3.status, "| 首页含标题:", "充电桩" in html)

    st4 = no_proxy().open(BASE + "/vendor/markers/none0.png", timeout=5)
    print("marker图标 HTTP", st4.status, "| bytes", len(st4.read()))

    print("\nPACKAGED EXE TEST: OK")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
