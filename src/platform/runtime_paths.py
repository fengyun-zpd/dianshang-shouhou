"""统一运行时路径（D 盘项目根 `.runtime/`；V1 存储约束）。

约束（AGENTS.md / V1 收口任务卡）：
- 所有新增下载、缓存、临时文件、checkpoint、测试临时目录只能落在 D 盘项目目录下；
- 默认根目录 = 项目根 `.runtime/`，提供 `tmp/`、`checkpoints/`、`cache/`；
- 禁止在 C 盘安装 Python/依赖/缓存或删除任何文件。

使用（scripts / evals / 演示 / 测试均可导入本模块，项目根已在 sys.path）：
    from src.platform.runtime_paths import runtime_dir, runtime_tmp_dir
    path = runtime_tmp_dir() / f"x-{pid}.sqlite"     # .runtime/tmp 下显式落文件

环境变量覆盖（可选）：`OPSPILOT_RUNTIME_ROOT` 可改根；默认项目根 `.runtime`。
目录按需惰性创建（mkdir parents=True, exist_ok=True），不污染工作树（已 gitignore）。
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]      # src/platform/runtime_paths.py → 项目根


def runtime_root() -> Path:
    """统一运行时根目录（默认 项目根/.runtime；可用环境变量覆盖）。"""
    override = os.environ.get("OPSPILOT_RUNTIME_ROOT")
    root = Path(override) if override else (PROJECT_ROOT / ".runtime")
    root.mkdir(parents=True, exist_ok=True)
    return root


def runtime_dir(name: str) -> Path:
    """返回 .runtime/<name> 子目录（惰性创建）。name ∈ {tmp, checkpoints, cache, …}。"""
    d = runtime_root() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def runtime_tmp_dir() -> Path:
    return runtime_dir("tmp")


def runtime_checkpoints_dir() -> Path:
    return runtime_dir("checkpoints")


def runtime_cache_dir() -> Path:
    return runtime_dir("cache")


__all__ = [
    "PROJECT_ROOT",
    "runtime_root",
    "runtime_dir",
    "runtime_tmp_dir",
    "runtime_checkpoints_dir",
    "runtime_cache_dir",
]
