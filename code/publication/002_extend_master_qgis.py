"""Extend the public master QGIS project with source-image control layers."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from osgeo import gdal
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsMarkerSymbol,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtGui import QColor


YEARS = (1922, 1925, 1928, 1934, 1936, 1938)


def checked_layer(layer, label: str):
    if not layer.isValid():
        raise RuntimeError(f"Invalid QGIS layer: {label} ({layer.source()})")
    return layer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    project_path = repo_root / "qgis" / "MOROCCO_HISTORICAL_MAPS_1922_1938.qgz"
    large_scan = repo_root / "sources" / "original_scans" / "C1938_CARTE_ORIGINALE.jpg"
    large_scan_display = (
        repo_root / "data" / "source_image_control" / "C1938_QGIS_DISPLAY.tif"
    )
    os.environ["JPEGMEM"] = "1024M"
    gdal.UseExceptions()
    translated = gdal.Translate(
        str(large_scan_display),
        str(large_scan),
        format="GTiff",
        creationOptions=[
            "TILED=YES",
            "COMPRESS=JPEG",
            "JPEG_QUALITY=85",
            "PHOTOMETRIC=YCBCR",
            "BIGTIFF=IF_SAFER",
        ],
    )
    if translated is None:
        raise RuntimeError(f"Cannot build {large_scan_display}")
    translated = None

    application = QgsApplication([], False)
    application.initQgis()
    try:
        project = QgsProject.instance()
        if not project.read(str(project_path)):
            raise RuntimeError(f"Cannot read {project_path}")
        project.setFilePathStorage(Qgis.FilePathType.Relative)
        root = project.layerTreeRoot()

        previous = root.findGroup("Source images — control in pixel coordinates")
        if previous is not None:
            project.removeMapLayers([node.layerId() for node in previous.findLayers()])
            root.removeChildNode(previous)
        source_root = root.insertGroup(0, "Source images — control in pixel coordinates")
        source_root.setItemVisibilityChecked(False)
        source_root.setExpanded(True)

        for year in YEARS:
            year_group = source_root.addGroup(str(year))
            year_group.setItemVisibilityChecked(False)
            year_group.setExpanded(False)
            gpkg = (
                repo_root
                / "data"
                / "source_image_control"
                / f"C{year}_SOURCE_IMAGE_VECTORS.gpkg"
            )
            scan = repo_root / "sources" / "original_scans" / f"C{year}_CARTE_ORIGINALE.jpg"
            display_raster = large_scan_display if year == 1938 else scan

            raster = checked_layer(
                QgsRasterLayer(
                    str(display_raster),
                    f"{year} — original scan",
                    "gdal",
                ),
                f"{year} original raster",
            )
            routes = checked_layer(
                QgsVectorLayer(
                    f"{gpkg}|layername=routes_{year}_carte",
                    f"{year} — routes on source image",
                    "ogr",
                ),
                f"{year} source-image routes",
            )
            localities = checked_layer(
                QgsVectorLayer(
                    f"{gpkg}|layername=localites_{year}_carte",
                    f"{year} — localities on source image",
                    "ogr",
                ),
                f"{year} source-image localities",
            )

            routes.renderer().symbol().setColor(QColor("#00ffff"))
            routes.renderer().symbol().setWidth(0.8)
            localities.setRenderer(
                localities.renderer().clone()
            )
            marker = QgsMarkerSymbol.createSimple(
                {
                    "name": "circle",
                    "color": "255,255,0,255",
                    "outline_color": "0,0,0,255",
                    "size": "2.4",
                }
            )
            localities.renderer().setSymbol(marker)

            for layer in (raster, routes, localities):
                project.addMapLayer(layer, False)
            year_group.addLayer(localities)
            year_group.addLayer(routes)
            year_group.addLayer(raster)

        project.setTitle("Morocco historical road maps, 1922–1938 — open science")
        if not project.write(str(project_path)):
            raise RuntimeError(f"Cannot write {project_path}")
        print(f"Saved {project_path} with {len(project.mapLayers())} layers")
    finally:
        QgsApplication.exitQgis()


if __name__ == "__main__":
    main()
