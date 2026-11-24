# 实测打包 exe 带采集：真正抓一次数据，验证完整可用
import os, subprocess, sys, time, json, urllib.request
from pathlib import Path

# 本脚本位于 tools/ 下，包目录在项目根的 dist_包/ 下
ROOT = Path(__file__).resolve().parent.parent
PKG = str(ROOT / "dist_包" / "中大深圳充电桩看板-一键启动包")
EXE = os.path.join(PKG, "SYSUChargeWatch.exe")
PORT = 8892
BASE = "http://127.0.0.1:%d" % PORT

def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))

proc = subprocess.Popen(
    [EXE, "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=PKG, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace",
    creationflags=0x00000200,
)
try:
    # 等首次采集完成（网络请求可能十几秒）
    deadline = time.time() + 90
    last = b""
    while time.time() < deadline:
        if proc.poll() is not None:
            print("EXE 退出:\n", (proc.stdout.read() or "")[:3000]); sys.exit(1)
        try:
            r = opener().open(BASE + "/api/state", timeout=3)
            last = r.read()
            d = json.loads(last.decode("utf-8"))
            cab = sum(len(s.get("cabinets", [])) for s in d.get("sites", []))
            if cab > 0:
                print("首次采集完成: 点位数", len(d.get("sites", [])), "电柜数", cab)
                # 汇总空闲/占用
                free = sum(c.get("chargeable", 0) for s in d["sites"] for c in s["cabinets"])
                print("空闲汇总 chargeable:", free)
                break
        except Exception as e:
            pass
        time.sleep(1)
    else:
        print("超时，最后状态:", last[:500]); sys.exit(1)

    # 检查页面与静态资源
    html = opener().open(BASE + "/", timeout=5).read().decode("utf-8", "replace")
    print("首页字节", len(html), "| 含凭证面板:", "auth-paste" in html)
    appjs = opener().open(BASE + "/app.js", timeout=5).read().decode("utf-8", "replace")
    print("app.js 含解析函数:", "parseCaptureText" in appjs)

    print("\nPACKAGED LIVE TEST: OK")
finally:
    proc.terminate()
    try: proc.wait(timeout=5)
    except Exception: proc.kill()
