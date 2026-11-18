#!/usr/bin/env python3
"""Vérifier un produit annuel créé par le moteur consolidé 1938."""
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
        for block in iter(lambda: stream.read(1024 * 1024), b""): digest.update(block)
    return digest.hexdigest().lower()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--config", required=True, type=Path); args = parser.parse_args(); config_path = args.config.resolve(strict=True); root = config_path.parent.parent; config = json.loads(config_path.read_text(encoding="utf-8")); year = int(config["map_year"]); output = (root / config["output_geopackage"]).resolve(strict=True); production = json.loads(output.with_suffix(".production.json").read_text(encoding="utf-8"))
    expected = {f"routes_{year}_carte", f"localites_{year}_carte", f"routes_{year}_geographiques", f"localites_{year}_geographiques", "routes_maroc_actuel", "localites_maroc_actuel", f"carte_originale_{year}", f"carte_{year}_georeferencee"}
    with sqlite3.connect(output) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]; tables = {row[0] for row in connection.execute("SELECT table_name FROM gpkg_contents")}
    if integrity != "ok" or tables != expected: raise RuntimeError(f"INTEGRITE_OU_TABLES_INCORRECTES:{integrity}:{sorted(tables)}")
    roads_map = gpd.read_file(output, layer=f"routes_{year}_carte"); roads_geo = gpd.read_file(output, layer=f"routes_{year}_geographiques"); localities_map = gpd.read_file(output, layer=f"localites_{year}_carte"); localities_geo = gpd.read_file(output, layer=f"localites_{year}_geographiques"); modern_localities = gpd.read_file(output, layer="localites_maroc_actuel")
    if len(roads_map) != len(roads_geo) or len(localities_map) != len(localities_geo) or len(localities_map) != len(modern_localities): raise RuntimeError("FILIATION_DES_COMPTES_INCOHERENTE")
    if set(localities_map.locality_id.astype(str)) != set(modern_localities.locality_id.astype(str)): raise RuntimeError("FILIATION_LOCALITES_INCOHERENTE")
    if not localities_map.effective_road_relation.fillna(False).all(): raise RuntimeError("LOCALITE_SANS_RELATION_ROUTIERE_EFFECTIVE")
    if int(localities_map.validation_humaine.sum()) != 0: raise RuntimeError("VALIDATION_HUMAINE_INVENTEE")
    if roads_geo.crs is None or roads_geo.crs.to_epsg() != 4326: raise RuntimeError("SCR_GEOGRAPHIQUE_INCORRECT")
    xmin, ymin, xmax, ymax = roads_geo.total_bounds
    if not (-15 < xmin < 1 and -15 < xmax < 2 and 20 < ymin < 40 and 20 < ymax < 40): raise RuntimeError(f"EMPRISE_GEOGRAPHIQUE_INATTENDUE:{[xmin,ymin,xmax,ymax]}")
    result = {"status": f"PASS_C{year}_FULL_MAP_TECHNICAL_VERIFICATION", "epistemic_status": "TECHNICALLY_COHERENT_AUTOMATIC_CANDIDATE_NOT_HUMAN_VALIDATED", "output": str(output), "output_sha256": sha256(output), "tables": sorted(tables), "counts": production["counts"], "geographic_extent": [float(xmin), float(ymin), float(xmax), float(ymax)]}; output.with_suffix(".verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
