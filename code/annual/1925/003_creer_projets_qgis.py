#!/usr/bin/env python3
"""Créer les projets QGIS et les aperçus visibles décrits par une configuration de carte."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsLayerTreeGroup,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsMarkerSymbol,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
)


def vector(gpkg: Path, table: str, title: str) -> QgsVectorLayer:
    layer = QgsVectorLayer(f"{gpkg}|layername={table}", title, "ogr")
    if not layer.isValid():
        raise RuntimeError(f"COUCHE_QGIS_INVALIDE:{table}")
    return layer


def raster(gpkg: Path, table: str, title: str) -> QgsRasterLayer:
    layer = QgsRasterLayer(f"GPKG:{gpkg.as_posix()}:{table}", title, "gdal")
    if not layer.isValid():
        raise RuntimeError(f"RASTER_QGIS_INVALIDE:{table}")
    return layer


def style_line(layer: QgsVectorLayer, color: str, width: float, opacity: float = 1.0) -> None:
    symbol = layer.renderer().symbol()
    symbol.setColor(QColor(color))
    symbol.setWidth(width)
    symbol.setOpacity(opacity)


def style_point(layer: QgsVectorLayer, color: str, size: float, opacity: float = 1.0) -> None:
    symbol = QgsMarkerSymbol.createSimple({"name": "circle", "color": color, "size": str(size), "outline_color": "white"})
    symbol.setOpacity(opacity)
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))


def add_to_group(project: QgsProject, group: QgsLayerTreeGroup, layer, visible: bool = True) -> None:
    project.addMapLayer(layer, False)
    node = group.addLayer(layer)
    node.setItemVisibilityChecked(visible)


def validate_project(path: Path, expected_layers: int) -> None:
    check = QgsProject()
    if not check.read(str(path)):
        raise RuntimeError(f"PROJET_QGIS_ILLISIBLE:{path}")
    layers = list(check.mapLayers().values())
    if len(layers) != expected_layers or not all(layer.isValid() for layer in layers):
        raise RuntimeError(f"PROJET_QGIS_INCOMPLET:{path}:{len(layers)}")


def combined_extent(layers) -> QgsRectangle:
    extent = QgsRectangle()
    extent.setMinimal()
    for layer in layers:
        extent.combineExtentWith(layer.extent())
    extent.scale(1.03)
    return extent


def render_png(path: Path, layers, extent: QgsRectangle, crs: QgsCoordinateReferenceSystem | None, width: int = 2400) -> None:
    ratio = max(0.2, min(5.0, extent.height() / max(extent.width(), 1e-12)))
    height = max(900, min(3000, round(width * ratio)))
    settings = QgsMapSettings()
    settings.setLayers(layers)
    settings.setBackgroundColor(QColor("white"))
    settings.setOutputSize(QSize(width, height))
    settings.setExtent(extent)
    if crs is not None and crs.isValid():
        settings.setDestinationCrs(crs)
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    image = job.renderedImage()
    path.parent.mkdir(parents=True, exist_ok=True)
    if image.isNull() or not image.save(str(path), "PNG"):
        raise RuntimeError(f"ECHEC_EXPORT_IMAGE:{path}")


def visual_spec(config: dict) -> dict:
    if "visual_delivery" in config:
        return config["visual_delivery"]
    year = int(config["map_year"])
    return {
        "historical_raster_layer": f"carte_originale_{year}",
        "georeferenced_raster_layer": f"carte_{year}_georeferencee",
        "historical_roads_layer": f"routes_{year}_carte",
        "historical_localities_layer": f"localites_{year}_carte",
        "geographic_roads_layer": f"routes_{year}_geographiques",
        "geographic_localities_layer": f"localites_{year}_geographiques",
        "current_roads_layer": "routes_maroc_actuel",
        "current_localities_layer": "localites_maroc_actuel",
        "preview_prefix": config["map_id"],
    }


def historical_delivery(gpkg: Path, target: Path, output_dir: Path, config: dict, spec: dict) -> list[Path]:
    year = int(config["map_year"])
    project = QgsProject.instance()
    project.clear()
    project.setFileName(str(target))
    project.setTitle(f"Carte du Maroc {year} — objets historiques sur le document original")
    root = project.layerTreeRoot()
    group = root.addGroup(f"Carte historique {year}")
    original = raster(gpkg, spec["historical_raster_layer"], f"Carte originale {year}")
    roads = vector(gpkg, spec["historical_roads_layer"], "Routes historiques retenues")
    localities = vector(gpkg, spec["historical_localities_layer"], "Localités historiques retenues")
    article_cities = vector(gpkg, spec["article_cities_map_layer"], "Villes de l’article — cercles validés") if spec.get("article_cities_map_layer") else None
    style_line(roads, "#d73027", 0.45)
    style_point(localities, "#2166ac", 2.4)
    if article_cities is not None:
        style_point(article_cities, "#7a0177", 3.2)
    add_to_group(project, group, original, True)
    add_to_group(project, group, roads, True)
    add_to_group(project, group, localities, True)
    if article_cities is not None:
        add_to_group(project, group, article_cities, True)
    project.setCustomVariables({"lecture": "Coordonnées du raster original; preuve documentaire primaire"})
    if not project.write(str(target)):
        raise RuntimeError(f"ECHEC_ECRITURE_PROJET:{target}")
    validate_project(target, 4 if article_cities is not None else 3)
    prefix = spec.get("preview_prefix", config["map_id"])
    extent = original.extent()
    products = [
        output_dir / f"{prefix}_VUE_ORIGINALE_ROUTES.png",
        output_dir / f"{prefix}_VUE_ORIGINALE_LOCALITES.png",
        output_dir / f"{prefix}_VUE_ORIGINALE_COMBINEE.png",
    ]
    render_png(products[0], [roads, original], extent, None)
    point_layers = [article_cities, localities] if article_cities is not None else [localities]
    render_png(products[1], [*point_layers, original], extent, None)
    render_png(products[2], [*point_layers, roads, original], extent, None)
    return products


def current_delivery(gpkg: Path, target: Path, output_dir: Path, config: dict, spec: dict) -> list[Path]:
    year = int(config["map_year"])
    project = QgsProject.instance()
    project.clear()
    project.setFileName(str(target))
    project.setTitle(f"Carte du Maroc {year} — positionnement géographique et références actuelles")
    crs = QgsCoordinateReferenceSystem("EPSG:4326")
    project.setCrs(crs)
    root = project.layerTreeRoot()
    historical = root.addGroup("Objets historiques géoréférencés")
    references = root.addGroup("Références actuelles — secondaires")
    original = raster(gpkg, spec["georeferenced_raster_layer"], f"Carte historique {year} géoréférencée")
    original.renderer().setOpacity(0.35)
    roads = vector(gpkg, spec["geographic_roads_layer"], "Routes historiques géoréférencées")
    localities = vector(gpkg, spec["geographic_localities_layer"], "Localités historiques géoréférencées")
    article_cities = vector(gpkg, spec["article_cities_geographic_layer"], "Villes de l’article — cercles validés") if spec.get("article_cities_geographic_layer") else None
    modern_roads = vector(gpkg, spec["current_roads_layer"], "Routes du Maroc actuel — référence")
    modern_localities = vector(gpkg, spec["current_localities_layer"], "Localités actuelles — référence")
    style_line(roads, "#d73027", 0.55)
    style_point(localities, "#2166ac", 2.6)
    if article_cities is not None:
        style_point(article_cities, "#7a0177", 3.2)
    style_line(modern_roads, "#969696", 0.15, 0.40)
    style_point(modern_localities, "#636363", 1.4, 0.45)
    add_to_group(project, historical, original, True)
    add_to_group(project, historical, roads, True)
    add_to_group(project, historical, localities, True)
    if article_cities is not None:
        add_to_group(project, historical, article_cities, True)
    add_to_group(project, references, modern_roads, True)
    add_to_group(project, references, modern_localities, True)
    project.setCustomVariables({"lecture": "Le calage historique est estimé; les références actuelles ne créent ni ne filtrent les objets historiques"})
    if not project.write(str(target)):
        raise RuntimeError(f"ECHEC_ECRITURE_PROJET:{target}")
    validate_project(target, 6 if article_cities is not None else 5)
    prefix = spec.get("preview_prefix", config["map_id"])
    preview = output_dir / f"{prefix}_VUE_MAROC_ACTUEL.png"
    visible = [article_cities, localities, roads, modern_localities, modern_roads, original] if article_cities is not None else [localities, roads, modern_localities, modern_roads, original]
    render_png(preview, visible, combined_extent([roads, localities]), crs)
    return [preview]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config_path = args.config.resolve(strict=True)
    root = config_path.parent.parent
    config = json.loads(config_path.read_text(encoding="utf-8"))
    gpkg = (root / config["output_geopackage"]).resolve(strict=True)
    historical = root / config["historical_qgis_project"]
    current = root / config["current_qgis_project"]
    output_dir = historical.parent
    spec = visual_spec(config)
    products = historical_delivery(gpkg, historical, output_dir, config, spec)
    products.extend(current_delivery(gpkg, current, output_dir, config, spec))
    result = {"status": "PASS_VISUAL_DELIVERY", "historical_project": str(historical), "current_project": str(current), "previews": [str(path) for path in products]}
    report = output_dir / f"{config['map_id']}_LIVRAISON_VISUELLE.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    QgsApplication.setPrefixPath("C:/Program Files/QGIS 3.44.13/apps/qgis-ltr", True)
    app = QgsApplication([], False)
    app.initQgis()
    try:
        main()
    finally:
        app.exitQgis()
