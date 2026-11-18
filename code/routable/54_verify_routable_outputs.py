#!/usr/bin/env python3
"""Verify annual routable-network packages and consolidate their receipts."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pyogrio
from path_utils import resolve_project_path


REQUIRED_SPATIAL_LAYERS = {
    "repaired_edges_map",
    "repaired_edges_geographic",
    "repair_connectors_map",
    "repair_connectors_geographic",
    "repaired_nodes_map",
    "repaired_nodes_geographic",
}
REQUIRED_ATTRIBUTE_TABLES = {"edge_lineage", "technical_validation", "repair_proposals"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(resolve_project_path(args.config).read_text(encoding="utf-8"))
    output_root = resolve_project_path(config["output_dir"])
    rows: list[dict[str, object]] = []
    for year_value in config["years"]:
        year = int(year_value)
        output = output_root / str(year) if config.get("separate_year_directories", False) else output_root
        receipt_path = output / f"C{year}_ROUTABLE_TOPOLOGY_RECEIPT.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        geopackage = resolve_project_path(receipt["output_geopackage"])
        if not geopackage.is_file():
            geopackage = output / f"C{year}_ROUTABLE_TOPOLOGY_REPAIRED.gpkg"
        checks = pd.read_csv(output / f"C{year}_ROUTABLE_TOPOLOGY_VALIDATION.csv")
        layers = set(pyogrio.list_layers(geopackage)[:, 0].tolist())
        with sqlite3.connect(geopackage) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        annual_rules = resolve_project_path(
            receipt.get("controlling_annual_road_rules", config["source_road_rules"][str(year)])
        )
        conditions = {
            "sqlite_integrity": integrity == "ok",
            "receipt_hash": sha256(geopackage).lower() == str(receipt["output_sha256"]).lower(),
            "source_topology_hash": sha256(resolve_project_path(receipt["source_topology"])).lower() == str(receipt["source_topology_sha256"]).lower(),
            "source_raster_hash": sha256(resolve_project_path(receipt["source_raster"])).lower() == str(receipt["source_raster_sha256"]).lower(),
            "annual_rules_present": annual_rules.is_file(),
            "required_spatial_layers": REQUIRED_SPATIAL_LAYERS.issubset(layers),
            "required_attribute_tables": REQUIRED_ATTRIBUTE_TABLES.issubset(tables) if year not in {1922, 1925} else True,
            "legacy_lineage_csv": (output / f"C{year}_ROUTABLE_TOPOLOGY_LINEAGE.csv").is_file() if year in {1922, 1925} else True,
            "legacy_repairs_csv": (output / f"C{year}_ROUTABLE_TOPOLOGY_REPAIRS.csv").is_file() if year in {1922, 1925} else True,
            "all_technical_checks": checks.status.eq("PASS").all(),
            "qgis_project": (output / f"C{year}_ROUTABLE_TOPOLOGY_CONTROL.qgs").is_file(),
            "overview": (output / f"C{year}_ROUTABLE_TOPOLOGY_OVERVIEW_HIGHRES.png").is_file(),
            "difficult_zooms": (output / f"C{year}_ROUTABLE_TOPOLOGY_REPAIR_ZOOMS_HIGHRES.png").is_file(),
        }
        if "controlling_annual_road_rules_sha256" in receipt:
            conditions["annual_rules_hash"] = sha256(annual_rules).lower() == str(receipt["controlling_annual_road_rules_sha256"]).lower()
        failed = [name for name, passed in conditions.items() if not passed]
        if failed:
            raise RuntimeError(f"C{year}:OUTPUT_VERIFICATION_FAILED:{','.join(failed)}")
        rows.append({
            "map_year": year,
            "initial_edges": receipt["initial_edges"],
            "repair_connectors": receipt["repair_connectors"],
            "repaired_edges": receipt["repaired_edges"],
            "initial_components": receipt["initial_components"],
            "repaired_components": receipt["repaired_components"],
            "initial_largest_component_nodes": receipt["initial_largest_component_nodes"],
            "repaired_largest_component_nodes": receipt["repaired_largest_component_nodes"],
            "technical_checks": len(checks),
            "output_sha256": receipt["output_sha256"],
            "status": receipt["status"],
            "legacy_receipt_rule_hash_unavailable": "controlling_annual_road_rules_sha256" not in receipt,
        })
    summary = pd.DataFrame(rows)
    summary.to_csv(output_root / "ROUTABLE_TOPOLOGY_REPAIR_SUMMARY.csv", index=False)
    verification = {
        "status": "PASS_TECHNICAL_OUTPUT_VERIFICATION",
        "years": summary.map_year.tolist(),
        "packages": rows,
        "scientific_status": "TECHNICALLY_VERIFIED_MAP_DERIVED_ROUTABLE_TOPOLOGY_PENDING_HUMAN_VISUAL_CONTROL",
    }
    (output_root / "ROUTABLE_TOPOLOGY_OUTPUT_VERIFICATION.json").write_text(json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(verification, ensure_ascii=False))


if __name__ == "__main__":
    main()
