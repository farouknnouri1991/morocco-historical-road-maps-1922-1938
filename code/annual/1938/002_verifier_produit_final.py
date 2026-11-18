#!/usr/bin/env python3
"""Vérifier le produit 1938 géographiquement unifié et sa filiation scientifique."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import CRS, Transformer
from shapely import affinity
from shapely.geometry import LineString, Point


EXPECTED_TABLES = {
    "routes_1938_carte",
    "localites_1938_carte",
    "routes_1938_geographiques",
    "localites_1938_geographiques",
    "routes_maroc_actuel",
    "localites_maroc_actuel",
    "distances_routieres_validees",
    "carte_originale_1938",
    "carte_1938_georeferencee",
}
AEQD = CRS.from_proj4("+proj=aeqd +lat_0=32 +lon_0=-6 +datum=WGS84 +units=m +no_defs")
TO_METRES = Transformer.from_crs(4326, AEQD, always_xy=True)
TO_WGS84 = Transformer.from_crs(AEQD, 4326, always_xy=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def m0_coefficients(human_path: Path, m0_path: Path) -> tuple[np.ndarray, np.ndarray]:
    controls = pd.read_csv(human_path).merge(pd.read_csv(m0_path), on="city_id", validate="one_to_one")
    pixels = controls[["map_pixel_x_candidate", "map_pixel_y_candidate"]].to_numpy(float)
    east, north = TO_METRES.transform(controls.lon.to_numpy(float), controls.lat.to_numpy(float))
    metric = np.c_[east, north]
    coefficients = np.linalg.lstsq(np.c_[np.ones(len(pixels)), pixels], metric, rcond=None)[0]
    residuals = np.linalg.norm(np.c_[np.ones(len(pixels)), pixels] @ coefficients - metric, axis=1)
    return coefficients, residuals


def source_down_to_geographic(geometry, coefficients: np.ndarray):
    coordinates = np.asarray(geometry.coords, float)
    metric = np.c_[np.ones(len(coordinates)), coordinates] @ coefficients
    lon, lat = TO_WGS84.transform(metric[:, 0], metric[:, 1])
    if len(coordinates) == 1:
        return Point(lon[0], lat[0])
    return LineString(zip(lon, lat))


def read_table(path: Path, table: str) -> pd.DataFrame:
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
        return pd.read_sql_query(f'SELECT * FROM "{table}"', connection).drop(columns="fid", errors="ignore")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config_path = args.config.resolve(strict=True)
    root = config_path.parent.parent
    config = json.loads(config_path.read_text(encoding="utf-8"))
    reference = (root / config["reference_geopackage"]).resolve(strict=True)
    candidate = (root / config["output_geopackage"]).resolve(strict=True)
    coefficients, residuals = m0_coefficients(root / config["human_locality_decisions"], root / config["m0_control_coordinates"])

    reference_roads = gpd.read_file(reference, layer="routes_1938_carte").set_index("edge_id").sort_index()
    candidate_map_roads = gpd.read_file(candidate, layer="routes_1938_carte").set_index("edge_id").sort_index()
    observed_roads = gpd.read_file(candidate, layer="routes_1938_geographiques").set_index("edge_id").sort_index()
    if list(reference_roads.index) != list(observed_roads.index) or list(reference_roads.index) != list(candidate_map_roads.index):
        raise RuntimeError("FILIATION_IDENTIFIANTS_ROUTES_ROMPUE")
    map_route_errors = shapely.hausdorff_distance(reference_roads.geometry.array, candidate_map_roads.geometry.array)
    if float(np.nanmax(map_route_errors)) > 1e-12:
        raise RuntimeError("GEOMETRIE_ROUTES_CARTE_DIFFERENTE")
    expected_roads = []
    for geometry in reference_roads.geometry:
        source_down = affinity.affine_transform(geometry, [1, 0, 0, -1, 0, float(config["height"])])
        expected_roads.append(source_down_to_geographic(source_down, coefficients))
    route_errors = shapely.hausdorff_distance(np.asarray(expected_roads, dtype=object), observed_roads.geometry.array)
    maximum_route_error = float(np.nanmax(route_errors))
    if maximum_route_error > 1e-12:
        raise RuntimeError(f"GEOMETRIE_ROUTES_M0_DIFFERENTE:{maximum_route_error}")

    reference_localities = gpd.read_file(reference, layer="localites_1938_carte").set_index("locality_id").sort_index()
    candidate_map_localities = gpd.read_file(candidate, layer="localites_1938_carte").set_index("locality_id").sort_index()
    observed_localities = gpd.read_file(candidate, layer="localites_1938_geographiques").set_index("locality_id").sort_index()
    if list(reference_localities.index) != list(observed_localities.index) or list(reference_localities.index) != list(candidate_map_localities.index):
        raise RuntimeError("FILIATION_IDENTIFIANTS_LOCALITES_ROMPUE")
    map_locality_errors = shapely.hausdorff_distance(reference_localities.geometry.array, candidate_map_localities.geometry.array)
    if float(np.nanmax(map_locality_errors)) > 1e-11:
        raise RuntimeError("GEOMETRIE_LOCALITES_CARTE_DIFFERENTE")
    expected_points = []
    for geometry in reference_localities.geometry:
        source_down = affinity.affine_transform(geometry, [1, 0, 0, -1, 0, float(config["height"])])
        expected_points.append(source_down_to_geographic(source_down, coefficients))
    locality_errors = shapely.hausdorff_distance(np.asarray(expected_points, dtype=object), observed_localities.geometry.array)
    maximum_locality_error = float(np.nanmax(locality_errors))
    if maximum_locality_error > 1e-12:
        raise RuntimeError(f"GEOMETRIE_LOCALITES_M0_DIFFERENTE:{maximum_locality_error}")

    expected_distances = read_table(reference, "distances_routieres_validees").astype(str)
    observed_distances = read_table(candidate, "distances_routieres_validees").astype(str)
    if not expected_distances.equals(observed_distances):
        raise RuntimeError("DISTANCES_VALIDEES_DIFFERENTES")

    with sqlite3.connect(candidate) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT table_name FROM gpkg_contents")}
        rasters = {row[0]: list(row) for row in connection.execute("SELECT table_name,min_x,min_y,max_x,max_y,srs_id FROM gpkg_contents WHERE data_type='tiles'")}
    if integrity != "ok" or tables != EXPECTED_TABLES:
        raise RuntimeError(f"INTEGRITE_OU_TABLES_INCORRECTES:{integrity}:{sorted(tables)}")
    if set(rasters) != {"carte_originale_1938", "carte_1938_georeferencee"} or rasters["carte_originale_1938"][-1] not in {-1, 99999} or rasters["carte_1938_georeferencee"][-1] != 4326:
        raise RuntimeError(f"RASTERS_INCORRECTS:{rasters}")
    human = [int(observed_localities.automatic_tier.eq("HUMAN_VALIDATED_CONTROL").sum()), int(observed_localities.telephone_1938.notna().sum()), int(observed_localities.essence_depot_1938.notna().sum())]
    if human != [28, 28, 28]:
        raise RuntimeError(f"RESULTATS_HUMAINS_INCOMPLETS:{human}")

    result = {
        "status": "PASS_PRODUIT_1938_GEOGRAPHICALLY_UNIFIED",
        "candidate_sha256": sha256(candidate),
        "tables": sorted(tables),
        "counts": {"routes_1938": len(observed_roads), "localites_1938": len(observed_localities)},
        "maximum_route_hausdorff_error_degrees": maximum_route_error,
        "maximum_locality_error_degrees": maximum_locality_error,
        "m0_control_residual_metres": {"mean": float(residuals.mean()), "maximum": float(residuals.max())},
        "human_results": human,
        "rasters": rasters,
        "sqlite_integrity": integrity,
    }
    proof = candidate.with_suffix(".verification.json")
    proof.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
