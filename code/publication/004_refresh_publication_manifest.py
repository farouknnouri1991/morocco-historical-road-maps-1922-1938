"""Refresh file checksums while preserving the layer-lineage section."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument(
        "--state", default="PREPARED_OPEN_SCIENCE_EXTENSION", help="Manifest state label"
    )
    args = parser.parse_args()
    root = args.repo_root.resolve()
    manifest_path = root / "PUBLICATION_MANIFEST.json"
    current = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts or path == manifest_path:
            continue
        if path.name.endswith(".aux.xml") or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix()
        files.append(
            {"path": relative, "bytes": path.stat().st_size, "sha256": sha256(path)}
        )
    refreshed = {
        "schema": current["schema"],
        "source_root": current["source_root"],
        "publication_root": current["publication_root"],
        "layers": current["layers"],
        "files": files,
        "publication_state": args.state,
    }
    manifest_path.write_text(
        json.dumps(refreshed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Recorded {len(files)} public files")


if __name__ == "__main__":
    main()
