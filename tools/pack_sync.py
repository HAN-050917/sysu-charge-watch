"""增量同步：把源码中的 web 静态资源 / 文档 / README 同步进已打好的一键启动包，
并重新压缩 zip。核心 exe 与 _internal 运行时不重建（Python 源码未改动时用）。

用法（在项目根目录执行）：python tools/pack_sync.py
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

# 本脚本位于 tools/ 下，项目根目录是其父目录
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist_包"
PKG_NAME = "中大深圳充电桩看板-一键启动包"
PKG = OUT / PKG_NAME


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()[:12]


def sync_file(src: Path, dst: Path) -> str:
    if not src.exists():
        return "SRC-MISSING"
    if dst.exists() and md5(src) == md5(dst):
        return "same"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return "UPDATED"


def main() -> None:
    if not PKG.exists():
        print("包目录不存在:", PKG)
        return

    print("=== 1. 同步 web 静态资源 ===")
    # 顶层 4 个文件
    for f in ("index.html", "app.js", "styles.css", "gz-swiss.css"):
        print(f"  {f}: {sync_file(ROOT/'web'/f, PKG/'web'/f)}")
    # vendor 整个目录（leaflet + markers），增量覆盖
    for sub in ("leaflet", "markers"):
        sdir = ROOT / "web" / "vendor" / sub
        ddir = PKG / "web" / "vendor" / sub
        changed = 0
        for f in sdir.glob("*"):
            if f.is_file() and sync_file(f, ddir / f.name) == "UPDATED":
                changed += 1
        print(f"  vendor/{sub}: {changed} 个更新 / 共 {len(list(sdir.glob('*')))}")

    print("=== 2. 同步 data ===")
    print(f"  sites.json: {sync_file(ROOT/'data'/'sites.json', PKG/'data'/'sites.json')}")
    # config.json 保留包内空模板（不含用户 token），不覆盖

    print("=== 3. 同步说明文档 ===")
    docdir = PKG / "说明文档"
    docdir.mkdir(exist_ok=True)
    for name in ("抓包获取Token指南.md", "分享与部署指南.md"):
        print(f"  {name}: {sync_file(ROOT/'docs'/name, docdir/name)}")

    print("=== 4. 同步根目录 README（源码 README）===")
    print(f"  README.md → 包内说明文档/README.md: {sync_file(ROOT/'README.md', docdir/'README.md')}")

    print("=== 5. 重新压缩 zip ===")
    zip_path = OUT / (PKG_NAME + ".zip")
    if zip_path.exists():
        zip_path.unlink()
    shutil.make_archive(str(OUT / PKG_NAME), "zip", OUT, PKG_NAME)
    print(f"  zip: {zip_path.relative_to(ROOT)}  {zip_path.stat().st_size/1048576:.1f} MB")

    print("=== 完成 ===")


if __name__ == "__main__":
    main()
