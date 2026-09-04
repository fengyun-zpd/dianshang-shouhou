"""分层回归脚本（Python 标准库实现，无第三方依赖）。

用法（项目根目录执行）：
    python scripts/run_tests.py            # 分层 + 全量回归
    python scripts/run_tests.py --quick    # 仅快速冒烟（现有核心用例）

设计要点：
- 显式 `-p no:cacheprovider`：规避中文路径下 pytest cache 写失败（R2）。
- 输出每层通过/失败/耗时；任何失败整体返回非 0 退出码（便于 CI）。
- 不硬编码用例数量；按目录探测，未来新增目录自动纳入。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 分层：unit 覆盖根 tests 现有用例 + tests/unit；其余按目录存在性探测。
LAYER_PATHS = {
    "unit": ["tests/unit", "tests/test_refund_service.py"],
    "integration": ["tests/integration"],
    "e2e": ["tests/e2e"],
    "property": ["tests/property"],
    "security": ["tests/security"],
}


def run_pytest(args: list[str]) -> tuple[int, float]:
    cmd = [sys.executable, "-m", "pytest", *args, "-p", "no:cacheprovider", "--tb=short"]
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description="分层回归脚本")
    parser.add_argument("--quick", action="store_true", help="仅快速冒烟（现有核心用例）")
    args = parser.parse_args()

    overall = 0
    if args.quick:
        overall, _ = run_pytest(["tests/test_refund_service.py", "tests/unit", "-q"])
    else:
        for layer in ("unit", "integration", "e2e", "property", "security"):
            paths = LAYER_PATHS[layer]
            if not any(os.path.exists(os.path.join(ROOT, p)) for p in paths):
                print(f"[{layer}] 无对应用例目录，跳过（属正常，非失败）")
                continue
            print(f"===== 分层：{layer} =====")
            code, elapsed = run_pytest([*paths, "-q"])
            print(f"[{layer}] 退出码={code}，耗时 {elapsed:.2f}s")
            overall = overall or code
        print("===== 全量回归 =====")
        code, elapsed = run_pytest(["tests/", "-q"])
        print(f"[full] 退出码={code}，耗时 {elapsed:.2f}s")
        overall = overall or code

    if overall == 0:
        print("REGRESSION PASS")
    else:
        print("REGRESSION FAIL")
    return overall


if __name__ == "__main__":
    sys.exit(main())
