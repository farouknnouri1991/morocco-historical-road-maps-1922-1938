#!/usr/bin/env python3
"""Vérifier l'intégrité technique et la cohérence interne du produit complet C1928."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import geopandas as gpd


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().lower()


def fail(message: str) -> None:
    raise RuntimeError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config_path = args.config.resolve(strict=True)
    root = config_path.parent.parent
    config = json.loads(config_path.read_text(encoding="utf-8"))
    year = int(config["map_year"])
    output = (root / config["output_geopackage"]).resolve(strict=True)
    production_path = output.with_suffix(".production.json")
    production = json.loads(production_path.read_text(encoding="utf-8"))
    expected = {
        f"routes_{year}_carte", f"localites_{year}_carte", f"textes_{year}_carte",
        f"routes_{year}_geographiques", f"localites_{year}_geographiques", f"textes_{year}_geographiques",
        "routes_maroc_actuel", "localites_maroc_actuel", f"carte_originale_{year}", f"carte_{year}_georeferencee",
    }
    with sqlite3.connect(output) as connection:
        tables = {row[0] for row in connection.execute("SELECT table_name FROM gpkg_contents")}
    if tables != expected:
        fail(f"TABLES_INATTENDUES:{sorted(tables)}")
    roads_map = gpd.read_file(output, layer=f"routes_{year}_carte")
    roads_geo = gpd.read_file(output, layer=f"routes_{year}_geographiques")
    localities_map = gpd.read_file(output, layer=f"localites_{year}_carte")
    localities_geo = gpd.read_file(output, layer=f"localites_{year}_geographiques")
    texts_map = gpd.read_file(output, layer=f"textes_{year}_carte")
    texts_geo = gpd.read_file(output, layer=f"textes_{year}_geographiques")
    modern_localities = gpd.read_file(output, layer="localites_maroc_actuel")
    frames = {"routes_map": roads_map, "routes_geo": roads_geo, "localities_map": localities_map, "localities_geo": localities_geo, "texts_map": texts_map, "texts_geo": texts_geo}
    for name, frame in frames.items():
        if frame.geometry.isna().any() or frame.geometry.is_empty.any():
            fail(f"GEOMETRIE_INVALIDE:{name}")
    counts = production["counts"]
    if len(roads_map) != counts["roads"] or len(roads_geo) != counts["roads"]:
        fail("NOMBRE_ROUTES_INCOHERENT")
    if len(localities_map) != counts["historical_localities"] or len(localities_geo) != counts["historical_localities"]:
        fail("NOMBRE_LOCALITES_INCOHERENT")
    if len(texts_map) != counts["text_objects"] or len(texts_geo) != counts["text_objects"]:
        fail("NOMBRE_TEXTES_INCOHERENT")
    if len(modern_localities) != len(localities_map):
        fail("CATALOGUE_ACTUEL_INCOMPLET_PAR_RAPPORT_AUX_LOCALITES_HISTORIQUES")
    if set(modern_localities.locality_id.astype(str)) != set(localities_map.locality_id.astype(str)):
        fail("FILIATION_LOCALITES_HISTORIQUES_ACTUELLES_INCOHERENTE")
    roles = roads_map.source_role.value_counts().to_dict()
    if int(roles.get("V1_FRAGMENT", 0)) != counts["road_fragments"] or int(roles.get("PI_RULE_CONNECTOR", 0)) != counts["road_connectors"]:
        fail("ROLES_ROUTIERS_INCOHERENTS")
    if counts["human_validated_localities"] != 0 or int(localities_map.validation_humaine.sum()) != 0:
        fail("VALIDATION_HUMAINE_INVENTEE")
    if roads_geo.crs is None or roads_geo.crs.to_epsg() != 4326:
        fail("SCR_GEOGRAPHIQUE_INCORRECT")
    xmin, ymin, xmax, ymax = roads_geo.total_bounds
    if not (-15 < xmin < 0 and -15 < xmax < 1 and 25 < ymin < 40 and 25 < ymax < 40):
        fail(f"EMPRISE_GEOGRAPHIQUE_INATTENDUE:{[xmin, ymin, xmax, ymax]}")
    if not roads_geo.georeference_status.astype(str).str.contains("AUTOMATIC_INHERITED_NOT_HUMAN_VALIDATED").all():
        fail("STATUT_GEOREFERENCE_INCORRECT")
    result = {
        "status": f"PASS_C{year}_FULL_MAP_TECHNICAL_VERIFICATION",
        "epistemic_status": "TECHNICALLY_COHERENT_AUTOMATIC_CANDIDATE_NOT_HUMAN_VALIDATED",
        "output": str(output),
        "output_sha256": sha256(output),
        "tables": sorted(tables),
        "counts": counts,
        "geographic_extent": [float(xmin), float(ymin), float(xmax), float(ymax)],
    }
    report = output.with_suffix(".verification.json")
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
