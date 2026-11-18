#!/usr/bin/env python3
"""Check the complete configured input/output routing without running analysis."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from path_utils import WORKSTREAM_ROOT, resolve_project_path


CONFIG_FILES = (
    "routable_topology_1922_1925.json",
    "routable_topology_1928_1938.json",
    "core28_measurement_from_routable_maps.json",
    "contemporary_core28_network_2026-10-11.json",
    "contemporary_all148_network_2026-10-11.json",
)
INPUT_KEYS = {
    "source_topology",
    "source_raster",
    "source_road_rules",
    "core28_csv",
    "functional_units_csv",
    "topology_gpkg",
    "osm_pbf",
    "historical_summary_csv",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iter_paths(config: dict[str, object]):
    for key in INPUT_KEYS:
        value = config.get(key)
        if isinstance(value, str):
            yield key, None, value
        elif isinstance(value, dict):
            for label, item in value.items():
                if isinstance(item, str):
                    yield key, str(label), item


def main() -> None:
    failures: list[str] = []
    checked: list[dict[str, object]] = []
    for name in CONFIG_FILES:
        config_path = WORKSTREAM_ROOT / "config" / name
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for key, label, raw in iter_paths(config):
            path = resolve_project_path(raw)
            exists = path.is_file()
            entry = {
                "config": name,
                "key": key,
                "label": label,
                "configured_path": raw,
                "resolved_path": str(path),
                "exists": exists,
            }
            if exists and path.suffix.lower() in {".csv", ".json"}:
                entry["sha256"] = sha256(path)
            checked.append(entry)
            if not exists:
                failures.append(f"{name}:{key}:{label or '-'}:{path}")
        output = resolve_project_path(str(config["output_dir"]))
        checked.append({
            "config": name,
            "key": "output_dir",
            "configured_path": config["output_dir"],
            "resolved_path": str(output),
            "parent_exists": output.parent.is_dir(),
        })
        if not output.parent.is_dir():
            failures.append(f"{name}:output_parent:{output.parent}")
    report = {
        "status": "PASS_PORTABLE_INPUT_PREFLIGHT" if not failures else "FAIL_PORTABLE_INPUT_PREFLIGHT",
        "workstream_root": str(WORKSTREAM_ROOT),
        "checks": checked,
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
