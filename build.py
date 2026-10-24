#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包成单文件 exe。

    python build.py            # 构建并放到项目根目录
    python build.py --keep     # 只构建到 dist/，不覆盖根目录的 exe

路径全部按本文件位置推导，换机器、换目录都不用改。
用完记得删掉 dist/ 与 build/（已在 .gitignore 里）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "WorkBuddyMonitor"
TARGET = ROOT / f"{NAME}.exe"
ASIDE = ROOT / f"{NAME}.exe.old"

REQUIRED = [
    ("dpi.manifest", "高 DPI 感知，漏了字体发糊"),
    ("workbuddy.ico", "窗口与任务栏图标"),
    ("web/index.html", "界面本体，漏了页面 500"),
    ("wbm_web.py", "入口"),
]


def check() -> None:
    missing = [(str(ROOT / n), why) for n, why in REQUIRED if not (ROOT / n).exists()]
    if missing:
        for path, why in missing:
            print(f"缺少 {path}（{why}）")
        raise SystemExit(1)
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit("缺少 PyInstaller，先执行：pip install pyinstaller")


def move_aside() -> None:
    """exe 正在运行时无法覆盖，但可以改名腾位（Windows 允许重命名运行中的 exe）。"""
    if not TARGET.exists():
        return
    try:
        if ASIDE.exists():
            ASIDE.unlink()
        TARGET.rename(ASIDE)
        print(f"旧 exe 已改名为 {ASIDE.name}（窗口关闭后即可删除）")
    except OSError as exc:
        raise SystemExit(f"旧 exe 既不能覆盖也不能改名：{exc}\n请先关闭 WorkBuddy Monitor 窗口。")


def main() -> None:
    check()
    keep = "--keep" in sys.argv

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--noconsole", "--noupx",
        "--name", NAME,
        "--manifest", str(ROOT / "dpi.manifest"),
        "--icon", str(ROOT / "workbuddy.ico"),
        "--add-data", f"{ROOT / 'web' / 'index.html'};web",
        "--add-data", f"{ROOT / 'workbuddy.ico'};.",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        str(ROOT / "wbm_web.py"),
    ]
    print("正在打包…")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，返回码 {result.returncode}")

    built = ROOT / "dist" / f"{NAME}.exe"
    if not built.exists():
        raise SystemExit(f"没找到产物 {built}")
    size = built.stat().st_size / 1024 / 1024
    if keep:
        print(f"完成：{built}（{size:.1f} MB）")
        return

    move_aside()
    shutil.copy2(built, TARGET)
    print(f"完成：{TARGET}（{size:.1f} MB）")
    print("dist/ 与 build/ 可删；正在运行的旧窗口关掉后，*.exe.old 也能删。")


if __name__ == "__main__":
    main()
