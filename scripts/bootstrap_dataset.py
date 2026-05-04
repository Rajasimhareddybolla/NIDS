from __future__ import annotations

import shutil
from pathlib import Path

import kagglehub


ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
DATASET_SLUG = "chethuhn/network-intrusion-dataset"

EXPECTED_FILES = [
    "Monday-WorkingHours.pcap_ISCX.csv",
    "Tuesday-WorkingHours.pcap_ISCX.csv",
    "Wednesday-workingHours.pcap_ISCX.csv",
    "Thursday-WorkingHours-Morning-WebAttacks.pcap_ISCX.csv",
    "Thursday-WorkingHours-Afternoon-Infilteration.pcap_ISCX.csv",
    "Friday-WorkingHours-Morning.pcap_ISCX.csv",
    "Friday-WorkingHours-Afternoon-DDos.pcap_ISCX.csv",
    "Friday-WorkingHours-Afternoon-PortScan.pcap_ISCX.csv",
]


def _build_lookup(files: list[Path]) -> dict[str, Path]:
    return {f.name.lower(): f for f in files}


def _copy_expected(download_root: Path, raw_dir: Path) -> tuple[list[str], list[str]]:
    all_csvs = [p for p in download_root.rglob("*.csv") if p.is_file()]
    lookup = _build_lookup(all_csvs)

    copied: list[str] = []
    missing: list[str] = []

    for expected in EXPECTED_FILES:
        src = lookup.get(expected.lower())
        if src is None:
            missing.append(expected)
            continue
        dst = raw_dir / expected
        shutil.copy2(src, dst)
        copied.append(expected)

    return copied, missing


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading dataset from KaggleHub: {DATASET_SLUG}")
    dataset_path = Path(kagglehub.dataset_download(DATASET_SLUG))
    print(f"Dataset cache path: {dataset_path}")

    copied, missing = _copy_expected(dataset_path, RAW_DIR)
    print(f"Copied {len(copied)} files to {RAW_DIR}")
    for name in copied:
        print(f"  - {name}")

    if missing:
        print("\nMissing expected files:")
        for name in missing:
            print(f"  - {name}")
        raise SystemExit(1)

    print("\nDataset bootstrap complete.")


if __name__ == "__main__":
    main()
