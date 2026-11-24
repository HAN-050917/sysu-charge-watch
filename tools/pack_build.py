"""一键打包脚本：把当前项目打包成可分发的「文件夹式一键启动包」。

用法（在项目根目录执行）：  python tools/pack_build.py
输出：  dist_包/中大深圳充电桩看板-一键启动包/
        （同目录下再压缩成 zip 方便分享）

要求：本机已 pip install pyinstaller（当前环境 6.22.3 可用）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# 本脚本位于 tools/ 下，项目根目录是其父目录
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist_包"
PKG_NAME = "中大深圳充电桩看板-一键启动包"
PKG = OUT / PKG_NAME
PY = sys.executable

CORE = ["app_paths.py", "server.py", "api_routes.py", "tracker.py",
        "tosun_client.py", "box_sampler.py", "target_model.py"]


def step(msg: str) -> None:
    print("\n=== " + msg + " ===", flush=True)


def main() -> int:
    step("0. 检查依赖")
    try:
        import PyInstaller  # noqa
        print("PyInstaller", PyInstaller.__version__)
    except ImportError:
        print("缺少 PyInstaller，请先 pip install pyinstaller")
        return 1

    step("1. 清理旧产物")
    if PKG.exists():
        shutil.rmtree(PKG)
    PKG.mkdir(parents=True, exist_ok=True)

    step("2. PyInstaller 打包单文件 exe")
    spec = ROOT / "_charge.spec"
    spec.write_text(make_spec(), encoding="utf-8")
    cmd = [PY, "-m", "PyInstaller", "--clean", "--noconfirm",
           "--distpath", str(OUT / "_build"), "--workpath", str(OUT / "_work"),
           str(spec)]
    print(" ".join(cmd), flush=True)
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print("PyInstaller 失败")
        return 1
    build_dir = OUT / "_build" / "SYSUChargeWatch"
    exe_src = build_dir / "SYSUChargeWatch.exe"
    if not exe_src.exists():
        print("找不到产物", exe_src)
        return 1
    # onedir：把 exe + _internal 运行时整个复制到包目录
    shutil.copy2(exe_src, PKG / "SYSUChargeWatch.exe")
    if (build_dir / "_internal").exists():
        shutil.copytree(build_dir / "_internal", PKG / "_internal", dirs_exist_ok=True)
    print("exe 大小 %.1f MB + _internal" % ((PKG / "SYSUChargeWatch.exe").stat().st_size / 1048576))

    step("3. 复制 web 资源")
    shutil.copytree(ROOT / "web", PKG / "web",
                    ignore=shutil.ignore_patterns(".selfcheck", "__pycache__", "*.pyc"))

    step("4. 复制 data 模板（不含敏感数据与历史库）")
    data = PKG / "data"
    data.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "data" / "sites.json", data / "sites.json")
    # 凭证配置留空模板；历史 watch.db 不带入（对方从零开始累积）
    (data / "config.json").write_text(
        '{\n  "poll_interval": 60,\n  "token": "",\n  "mobile": "",\n  "scan_distance_km": 3\n}\n',
        encoding="utf-8")

    step("5. 写启动器与说明")
    (PKG / "启动看板.bat").write_text(make_bat(), encoding="utf-8-sig")
    (PKG / "启动看板-备用端口.bat").write_text(make_bat(port=8788), encoding="utf-8-sig")
    (PKG / "README-先看我.txt").write_text(make_readme(), encoding="utf-8")
    # 附带完整文档：抓包教程 + 分享部署指南
    docdir = PKG / "说明文档"
    docdir.mkdir(exist_ok=True)
    for name in ("抓包获取Token指南.md", "分享与部署指南.md"):
        src = ROOT / "docs" / name
        if src.exists():
            shutil.copy2(src, docdir / name)
            print("附带文档:", name)

    step("6. 压缩成 zip")
    zip_path = OUT / (PKG_NAME + ".zip")
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(OUT / PKG_NAME), "zip", OUT, PKG_NAME)
    print("zip:", zip_path, "%.1f MB" % (zip_path.stat().st_size / 1048576))

    step("完成")
    print("交付目录:", PKG)
    print("分享文件:", zip_path)
    return 0


def make_spec() -> str:
    # spec 用 %r 需要转义，这里用 repr 处理路径
    scripts = ",\n        ".join(repr(str(ROOT / f)) for f in CORE)
    return f'''# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    [{scripts}],
    pathex=[{repr(str(ROOT))}],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='SYSUChargeWatch',
    debug=False, bootloader_ignore_signals=False, strip=False,
    upx=False, console=True, disable_windowed_traceback=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[],
    name='SYSUChargeWatch',
)
'''


def make_bat(port: int = 8787) -> str:
    return f'''@echo off
chcp 65001 >nul
title 中大深圳充电桩看板
cd /d "%~dp0"
echo.
echo   ============================================================
echo    中大深圳校区 · 充电桩实时空位看板
echo   ============================================================
echo.
echo   正在启动，请勿关闭此窗口...
echo.
start "" http://127.0.0.1:{port}/
"%~dp0SYSUChargeWatch.exe" --host 0.0.0.0 --port {port}
if errorlevel 1 (
  echo.
  echo   [启动失败] 端口 {port} 可能被占用。
  pause
)
'''


def make_readme() -> str:
    return r'''=====================================================================
 中大深圳校区 · 充电桩实时空位看板  ——  使用说明
=====================================================================

【这是什么】
 一个实时查看中山大学深圳校区各充电点「空闲桩位 / 使用中桩位 /
 已充时长 / 预计转空闲」的本地看板。数据来自青春号平台公开接口，
 只读，不涉及登录下单、支付、启停充电。

【怎么运行 —— 电脑】
 1. 把整个文件夹解压到任意位置（路径不要有中文也可以，但建议简单）。
 2. 双击「启动看板.bat」。会弹出一个黑色命令行窗口，不要关它。
 3. 浏览器会自动打开 http://127.0.0.1:8787/
    如果没自动打开，手动复制这个地址到浏览器。
 4. 关闭方法：回到那个黑色窗口，按 Ctrl+C，或直接关掉窗口。

【怎么在手机上看】
 1. 手机和电脑必须连【同一个 WiFi】。
 2. 电脑启动看板后，黑色窗口里会显示类似：
        局域网地址: http://172.29.xx.xx:8787/
    （不同网络这个 IP 会变，以窗口里显示的为准）
 3. 手机浏览器打开这个地址即可，界面已适配手机。
 注意：手机是【通过 WiFi 访问电脑】来看的，不是手机自己运行程序。
       电脑必须保持开机、看板窗口保持打开。

【端口被占用怎么办】
 如果双击后提示端口 8787 被占用，把「启动看板.bat」里的 8787
 改成 8788（两处都要改），然后浏览器改访问 http://127.0.0.1:8788/。

【关于「逐桩位真实已充时长」—— 可选增强】
 - 默认（不配置任何东西）即可看到：每个点位空闲/占用/故障数量、
   按现场位置编号的桩位组、以及基于历史统计的「预计转空闲」时间。
 - 配置凭证后，可解锁【每个桩位真实的已充时长、开始时间、电量、
   电压电流】——这些来自平台登录接口，需要手机抓包获取一次。
 - 获取方法（约 3 分钟）：
   1) 电脑装 Fiddler Classic，开启 HTTPS 抓包并安装其根证书；
   2) 手机 WiFi 代理指向电脑 Fiddler 的 8888 端口；
   3) 微信里打开一次「青春号TOSUN」小程序并进入充电页面；
   4) 在 Fiddler 里找任意一条 cloud.tosunelectric.com 的请求，
      把整条请求（含请求头）复制；
   5) 在看板页面「登录凭证」区，粘贴进「一键粘贴」框，点「识别并填入」，
      再点「保存并验证」即可。
   - 平台没有"手机号+短信验证码"网页登录（该接口已下线，
     小程序登录强依赖微信 getPhoneNumber），所以只能抓包这一次。
   - 完整图文教程见本文件夹「说明文档\抓包获取Token指南.md」，
     从装证书到贴进看板一步步都有；分享/部署见「分享与部署指南.md」。
 - token 只存在本文件夹 data\config.json，不上传、不进浏览器存储。

【文件说明】
 SYSUChargeWatch.exe  —— 程序主体
 启动看板.bat          —— 一键启动器
 web\                  —— 网页界面（含离线地图瓦片与平台图标）
 data\sites.json       —— 校区与点位配置（高级用户可改坐标）
 data\config.json      —— 你的凭证配置（可清空回到公开模式）
 data\watch.db         —— 运行后自动生成的历史数据（卸载可直接删）

【隐私与安全】
 - 全程只读公开接口，不登录下单、不支付、不控制充电设备。
 - 凭证仅保存在你自己的电脑上。
=====================================================================
'''


if __name__ == "__main__":
    raise SystemExit(main())
