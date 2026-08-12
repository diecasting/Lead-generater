#!/usr/bin/env python3
"""
init_lead_filter.py — 为任意新项目一键初始化「B2B 反同行 + 买方闸门」过滤引擎。

把本 Skill 打包好的两个资产文件复制到目标项目：
  <target>/lead_filter_engine.py            —— 核心过滤模块（零重依赖，仅 re/sys）
  <target>/tests/test_lead_filter_engine.py —— 单元测试（34+ 项）

复制完成后自动跑一遍模块自测（python lead_filter_engine.py），
若环境装了 pytest 再跑一遍单元测试，确保落地即可用。

用法：
  python init_lead_filter.py [TARGET_DIR] [--no-test]

  TARGET_DIR  目标项目根目录（默认：当前工作目录）。
  --no-test   跳过自测与 pytest（仅复制文件）。
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSET_MODULE = SKILL_DIR / "assets" / "lead_filter_engine.py"
ASSET_TEST = SKILL_DIR / "assets" / "test_lead_filter_engine.py"


def _find_python():
    """优先使用 managed python，找不到回退到 PATH 中的 python3/python。"""
    for cand in (
        r"C:\Users\anson\.workbuddy\binaries\python\versions\3.13.12\python.exe",
        "python3",
        "python",
    ):
        exe = shutil.which(cand) or (cand if Path(cand).exists() else None)
        if exe:
            return exe
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="初始化 B2B lead_filter_engine 到目标项目")
    parser.add_argument(
        "target", nargs="?", default=".",
        help="目标项目根目录（默认当前目录）")
    parser.add_argument(
        "--no-test", action="store_true",
        help="仅复制文件，跳过自测与 pytest")
    args = parser.parse_args(argv)

    target = Path(args.target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    tests_dir = target / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    if not ASSET_MODULE.exists() or not ASSET_TEST.exists():
        print(f"[init] 资产缺失：请确认 {ASSET_MODULE} 与 {ASSET_TEST} 存在。",
              file=sys.stderr)
        return 2

    dest_module = target / "lead_filter_engine.py"
    dest_test = tests_dir / "test_lead_filter_engine.py"
    shutil.copyfile(ASSET_MODULE, dest_module)
    shutil.copyfile(ASSET_TEST, dest_test)
    print(f"[init] 已写入 {dest_module}")
    print(f"[init] 已写入 {dest_test}")

    if args.no_test:
        print("[init] 已跳过测试（--no-test）。")
        return 0

    py = _find_python()
    if not py:
        print("[init] 未找到 python，跳过测试。", file=sys.stderr)
        return 0

    print("\n--- 模块自测（python lead_filter_engine.py）---")
    rc = subprocess.call([py, str(dest_module)])
    if rc != 0:
        print("[init] 模块自测失败，请检查复制结果。", file=sys.stderr)
        return rc

    # 尝试跑 pytest；装了才跑，没装就跳过（不视为失败）
    pytest_exe = shutil.which("pytest")
    if pytest_exe:
        print("\n--- 单元测试（pytest tests/test_lead_filter_engine.py）---")
        rc = subprocess.call([pytest_exe, "-q", str(dest_test)])
        if rc != 0:
            print("[init] 单元测试未全部通过，请排查。", file=sys.stderr)
            return rc
    else:
        print("\n[init] 环境未安装 pytest，已跳过单元测试（模块自测已通过）。")

    print("\n[init] 完成。可在项目中 `from lead_filter_engine import filter_leads` 直接使用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
