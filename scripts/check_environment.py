from __future__ import annotations

import importlib.util
import json
import platform
import shutil
from pathlib import Path


PY_MODULES = [
    "pandas",
    "numpy",
    "sklearn",
    "xgboost",
    "pyspark",
    "kafka",
    "pymongo",
    "motor",
    "fastapi",
    "jupyter",
]

BINARIES = [
    "java",
    "spark-submit",
    "pyspark",
    "kafka-server-start",
    "mongod",
]


def status(flag: bool) -> str:
    return "OK" if flag else "MISSING"


def main() -> None:
    report = {
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "modules": {},
        "binaries": {},
    }

    for module in PY_MODULES:
        report["modules"][module] = status(importlib.util.find_spec(module) is not None)

    for binary in BINARIES:
        report["binaries"][binary] = status(shutil.which(binary) is not None)

    out_dir = Path("logs")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "environment_report.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    print(f"\nSaved report to: {out_path}")


if __name__ == "__main__":
    main()
