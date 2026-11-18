"""Verify the public scans, source-image controls, and master QGIS project."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

from qgis.core import QgsApplication, QgsMapRendererParallelJob, QgsMapSettings, QgsProject
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor


YEARS = (1922, 1925, 1928, 1934, 1936, 1938)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--qa-dir", type=Path)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    manifest = json.loads(
        (repo_root / "sources" / "ORIGINAL_SCAN_TECHNICAL_MANIFEST.json").read_text(
            encoding="utf-8"
        )
    )
    by_year = {record["year"]: record for record in manifest}
    failures = []
    oversized = [
        path.relative_to(repo_root).as_posix()
        for path in repo_root.rglob("*")
        if path.is_file() and ".git" not in path.parts and path.stat().st_size >= 50 * 1024 * 1024
    ]
    if oversized:
        failures.append(f"files at or above 50 MiB: {oversized}")

    for year in YEARS:
        record = by_year[year]
        scan = repo_root / record["scan"]
        world_file = repo_root / record["world_file"]
        gpkg = repo_root / record["source_image_geopackage"]
        if sha256(scan) != record["scan_sha256"]:
            failures.append(f"{year}: scan checksum mismatch")
        if sha256(gpkg) != record["source_image_geopackage_sha256"]:
            failures.append(f"{year}: source-image GeoPackage checksum mismatch")
        if sha256(world_file) != record["world_file_sha256"]:
            failures.append(f"{year}: source-image world file checksum mismatch")
        connection = sqlite3.connect(gpkg)
        try:
            contents = {
                row[0]
                for row in connection.execute(
                    "SELECT table_name FROM gpkg_contents ORDER BY table_name"
                )
            }
        finally:
            connection.close()
        expected = set(record["source_layers"])
        if contents != expected:
            failures.append(
                f"{year}: unexpected GeoPackage contents {sorted(contents)}"
            )

    application = QgsApplication([], False)
    application.initQgis()
    try:
        project = QgsProject.instance()
        project_path = repo_root / "qgis" / "MOROCCO_HISTORICAL_MAPS_1922_1938.qgz"
        with zipfile.ZipFile(project_path) as archive:
            qgs_name = next(name for name in archive.namelist() if name.endswith(".qgs"))
            qgs_text = archive.read(qgs_name).decode("utf-8")
        if "C:\\Volumes\\FaroukData" in qgs_text or "Desktop" in qgs_text:
            failures.append("master QGIS project contains a private absolute path")
        if not project.read(str(project_path)):
            failures.append("master QGIS project could not be read")
        invalid = [layer.name() for layer in project.mapLayers().values() if not layer.isValid()]
        if invalid:
            failures.append(f"invalid QGIS layers: {invalid}")
        if len(project.mapLayers()) != 30:
            failures.append(f"expected 30 QGIS layers, found {len(project.mapLayers())}")
        source_group = project.layerTreeRoot().findGroup(
            "Source images — control in pixel coordinates"
        )
        if source_group is None:
            failures.append("source-image control group is missing")

        if args.qa_dir and source_group is not None:
            qa_dir = args.qa_dir.resolve()
            qa_dir.mkdir(parents=True, exist_ok=True)
            for year in YEARS:
                group = source_group.findGroup(str(year))
                layers = [node.layer() for node in group.findLayers()]
                raster = next(layer for layer in layers if "original scan" in layer.name())
                settings = QgsMapSettings()
                settings.setLayers(layers)
                settings.setBackgroundColor(QColor("white"))
                settings.setOutputSize(QSize(1600, 1100))
                settings.setExtent(raster.extent())
                renderer = QgsMapRendererParallelJob(settings)
                renderer.start()
                renderer.waitForFinished()
                renderer.renderedImage().save(str(qa_dir / f"qgis_source_overlay_{year}.png"))
    finally:
        QgsApplication.exitQgis()

    if failures:
        raise SystemExit("\n".join(failures))
    print("PASS: six scans, six source-image GeoPackages, and 30 QGIS layers verified")


if __name__ == "__main__":
    main()
