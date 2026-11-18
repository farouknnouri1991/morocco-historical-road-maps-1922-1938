#!/usr/bin/env python3
"""Adapter le moteur consolidé 1938 à un autre millésime sans changer sa logique scientifique."""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import cv2
import geopandas as gpd
import numpy as np
import pandas as pd
from PIL import Image
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("moteur_1938", HERE / "001_pipeline_consolide_1938.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("MOTEUR_1938_INTROUVABLE")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def configure_engine(config: dict, rules: dict) -> None:
    M.FRAME = tuple(config["map_frame"])
    excluded = list(rules.get("excluded_rectangles", {}).values())
    empty = (-1, -1, -1, -1)
    M.TITLE_CORE = tuple(excluded[0]) if excluded else empty
    M.LEGEND = tuple(excluded[1]) if len(excluded) > 1 else M.TITLE_CORE
    M.DISTANCE_TABLE = tuple(rules.get("distance_table", empty))
    M.WORK_WIDTH = int(config.get("work_width", min(6000, int(config["width"]))))


def paths_for(root: Path, config: dict) -> dict[str, Path]:
    keys = {
        "raster": "raster",
        "road_rules": "road_extraction_config",
        "controls": "automatic_georeference_controls",
        "geonames": "geonames",
        "osm": "osm_pbf",
        "tesseract": "tesseract_executable",
    }
    paths = {name: (root / config[key]).resolve() for name, key in keys.items()}
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"ENTREES_MANQUANTES:{missing}")
    return paths


def tiles_from_rules(width: int, height: int, rules: dict) -> list[dict]:
    size = int(rules.get("tiling", {}).get("tile_size", 1024))
    stride = int(rules.get("tiling", {}).get("stride", 896))
    xs = list(range(0, max(1, width - size + 1), stride))
    ys = list(range(0, max(1, height - size + 1), stride))
    if not xs or xs[-1] != max(0, width - size): xs.append(max(0, width - size))
    if not ys or ys[-1] != max(0, height - size): ys.append(max(0, height - size))
    return [{"grid_column": c + 1, "grid_row": r + 1, "x0": x, "y0": y, "x1": min(width, x + size), "y1": min(height, y + size)} for r, y in enumerate(ys) for c, x in enumerate(xs)]


def rename_engine_ids(roads: gpd.GeoDataFrame, map_id: str, year: int) -> gpd.GeoDataFrame:
    result = roads.copy()
    result["source_feature_id"] = [f"{map_id}_{'RV1' if role == 'V1_FRAGMENT' else 'GAP'}_REPRO_{index:07d}" for index, role in enumerate(result.source_role, 1)]
    result["edge_id"] = [f"{map_id}_REPRO_OBS_{index:07d}" for index in range(1, len(result) + 1)]
    result["map_year"] = year
    result["from_node"] = [f"{map_id}_NODE_{index:07d}_A" for index in range(1, len(result) + 1)]
    result["to_node"] = [f"{map_id}_NODE_{index:07d}_B" for index in range(1, len(result) + 1)]
    result["admissibility_status"] = f"{map_id}_SOURCE_FIRST_AUTOMATIC_CANDIDATE"
    result["source_method"] = "C1938_OPTIMIZED_RED_SOURCE_PIXELS_WITH_PI_APPROVED_GAP_CONNECTORS"
    return result


def attach_road_relations(circles: pd.DataFrame, roads: gpd.GeoDataFrame, source_width: int) -> pd.DataFrame:
    result = circles.copy()
    if result.empty:
        result["effective_road_relation"] = pd.Series(dtype=bool)
        return result
    observed = unary_union(roads.loc[roads.source_role.eq("V1_FRAGMENT"), "geometry"])
    connectors = unary_union(roads.loc[roads.source_role.eq("PI_RULE_CONNECTOR"), "geometry"])
    points = [Point(x, y) for x, y in zip(result.pixel_x, result.pixel_y)]
    result["circle_radius_source_px"] = result.work_radius * source_width / M.WORK_WIDTH
    result["observed_road_distance_px"] = [point.distance(observed) for point in points]
    result["connector_distance_px"] = [point.distance(connectors) for point in points]
    observed_relation = result.observed_road_distance_px <= 2.0 * result.circle_radius_source_px + 3.0
    connector_relation = result.connector_distance_px <= result.circle_radius_source_px + 3.0
    result["effective_road_relation"] = observed_relation | connector_relation
    result["road_relation_type"] = np.select([observed_relation, connector_relation], ["OBSERVED_ROAD", "PI_RULE_CONNECTOR_COUNTS_AS_ROAD"], default="NO_EFFECTIVE_ROAD_RELATION")
    return result


def fit_controls(path: Path) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    controls = pd.read_csv(path)
    pixels = controls[["map_pixel_x_candidate", "map_pixel_y_candidate"]].to_numpy(float)
    east, north = M.TO_METRES.transform(controls.lon.to_numpy(), controls.lat.to_numpy())
    metric = np.c_[east, north]
    return controls, M.affine_fit(pixels, metric), M.affine_fit(metric, pixels)


def automatic_catalog(circles: pd.DataFrame, labels: pd.DataFrame, controls_path: Path, geonames_path: Path, map_id: str) -> pd.DataFrame:
    _, forward, inverse = fit_controls(controls_path)
    gazetteer = M.read_geonames(geonames_path)
    east, north = M.TO_METRES.transform(gazetteer.longitude.to_numpy(), gazetteer.latitude.to_numpy())
    gazetteer["east"], gazetteer["north"] = east, north
    gazetteer_tree = cKDTree(gazetteer[["east", "north"]].to_numpy(float))
    alias_cache = [M.aliases(row) for _, row in gazetteer.iterrows()]
    ocr = labels.dropna(subset=["center_x", "center_y", "ocr_text"]).copy()
    ocr["ocr_norm"] = ocr.ocr_text.map(M.normalise_name)
    ocr = ocr[ocr.ocr_norm.str.len() >= 3].copy()
    eligible = circles[(circles.dark_ring_angular_sectors_36 >= 16) & (circles.nearest_label_distance_px <= 550) & circles.effective_road_relation].copy()
    columns = ["circle_candidate_id", "label_candidate_id", "historical_ocr_text", "geonameid", "gazetteer_name", "gazetteer_ascii_name", "gazetteer_feature_code", "gazetteer_population", "longitude", "latitude", "map_pixel_x", "map_pixel_y", "name_similarity", "label_geographic_distance_km", "circle_to_expected_pixel_distance", "circle_to_label_distance_px", "circle_radius_source_px", "observed_road_distance_px", "connector_distance_px", "effective_road_relation", "road_relation_type", "automatic_tier", "status", "rank_score"]
    if ocr.empty or eligible.empty:
        return pd.DataFrame(columns=[*columns, "locality_id"])
    label_metric = M.affine_predict(ocr[["center_x", "center_y"]].to_numpy(float), forward)
    circle_tree = cKDTree(eligible[["pixel_x", "pixel_y"]].to_numpy(float))
    proposals = []
    for position, (_, label) in enumerate(ocr.iterrows()):
        nearby = gazetteer_tree.query_ball_point(label_metric[position], r=40000)
        scored = []
        for gazetteer_index in nearby:
            name_score = max((M.similarity(label.ocr_norm, alias) for alias in alias_cache[gazetteer_index]), default=0.0)
            if name_score < 0.52: continue
            geo_distance = float(np.linalg.norm(gazetteer.iloc[gazetteer_index][["east", "north"]].to_numpy(float) - label_metric[position]) / 1000)
            scored.append((name_score - 0.006 * geo_distance, name_score, geo_distance, gazetteer_index))
        if not scored: continue
        _, name_score, geo_distance, gazetteer_index = max(scored)
        gazetteer_row = gazetteer.iloc[gazetteer_index]
        expected = M.affine_predict(np.array([[gazetteer_row.east, gazetteer_row.north]]), inverse)[0]
        candidates = circle_tree.query_ball_point(expected, r=360)
        candidates = [index for index in candidates if np.linalg.norm(eligible.iloc[index][["pixel_x", "pixel_y"]].to_numpy(float) - label[["center_x", "center_y"]].to_numpy(float)) <= 550]
        if not candidates: continue
        best = min(candidates, key=lambda index: np.linalg.norm(eligible.iloc[index][["pixel_x", "pixel_y"]].to_numpy(float) - expected) - 18 * eligible.iloc[index].raw_score)
        circle = eligible.iloc[best]
        pixel_distance = float(np.linalg.norm(circle[["pixel_x", "pixel_y"]].to_numpy(float) - expected))
        label_distance = float(np.linalg.norm(circle[["pixel_x", "pixel_y"]].to_numpy(float) - label[["center_x", "center_y"]].to_numpy(float)))
        if name_score >= 0.72 and geo_distance <= 20 and pixel_distance <= 220: tier = "HIGH_AUTOMATIC"
        elif name_score >= 0.55 and geo_distance <= 40 and pixel_distance <= 360: tier = "MEDIUM_AUTOMATIC"
        else: continue
        proposals.append({"circle_candidate_id": circle.candidate_id, "label_candidate_id": label.candidate_id, "historical_ocr_text": label.ocr_text, "geonameid": int(gazetteer_row.geonameid), "gazetteer_name": gazetteer_row["name"], "gazetteer_ascii_name": gazetteer_row.asciiname, "gazetteer_feature_code": gazetteer_row.feature_code, "gazetteer_population": int(gazetteer_row.population) if pd.notna(gazetteer_row.population) else 0, "longitude": gazetteer_row.longitude, "latitude": gazetteer_row.latitude, "map_pixel_x": circle.pixel_x, "map_pixel_y": circle.pixel_y, "name_similarity": name_score, "label_geographic_distance_km": geo_distance, "circle_to_expected_pixel_distance": pixel_distance, "circle_to_label_distance_px": label_distance, "circle_radius_source_px": circle.circle_radius_source_px, "observed_road_distance_px": circle.observed_road_distance_px, "connector_distance_px": circle.connector_distance_px, "effective_road_relation": True, "road_relation_type": circle.road_relation_type, "automatic_tier": tier, "status": "AUTOMATIC_MAP_CIRCLE_PLUS_MAP_LABEL_PLUS_GEONAMES_PLUS_EFFECTIVE_ROAD_RELATION", "rank_score": name_score * 6 - geo_distance / 35 - pixel_distance / 240 + np.log1p(max(0, gazetteer_row.population)) * 0.12})
    automatic = pd.DataFrame(proposals, columns=columns)
    if not automatic.empty:
        automatic = automatic.sort_values("rank_score", ascending=False).drop_duplicates("circle_candidate_id").drop_duplicates("geonameid").drop_duplicates("label_candidate_id")
    automatic = automatic.reset_index(drop=True)
    automatic["locality_id"] = [f"{map_id}_LOCALITY_{index:04d}" for index in range(1, len(automatic) + 1)]
    return automatic


def sea_mask(rgb: np.ndarray, controls_path: Path, config: dict) -> np.ndarray:
    factor = int(config["sea_mask"]["downsample_factor"])
    small = cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), ((rgb.shape[1] + factor - 1) // factor, (rgb.shape[0] + factor - 1) // factor), interpolation=cv2.INTER_AREA).astype(np.float32)
    def sample(x: float, y: float) -> np.ndarray:
        yy, xx = np.ogrid[:small.shape[0], :small.shape[1]]; ring = ((xx - x) ** 2 + (yy - y) ** 2 >= 9) & ((xx - x) ** 2 + (yy - y) ** 2 <= 64); return small[ring]
    sea_points, sea_samples = [], []
    for x0, y0, x1, y1 in config["sea_mask"]["seed_boxes"]:
        x, y = (x0 + x1) / 2 / factor, (y0 + y1) / 2 / factor; sea_points.append((int(round(x)), int(round(y)))); sea_samples.append(sample(x, y))
    controls = pd.read_csv(controls_path)
    land_samples = [sample(row.map_pixel_x_candidate / factor, row.map_pixel_y_candidate / factor) for row in controls.itertuples()]
    sea_value = np.concatenate(sea_samples)[:, 1] - np.concatenate(sea_samples)[:, 2]
    land_value = np.concatenate(land_samples)[:, 1] - np.concatenate(land_samples)[:, 2]
    threshold = float((np.median(sea_value) + np.median(land_value)) / 2)
    sea_like = ((small[:, :, 1] - small[:, :, 2]) >= threshold).astype(np.uint8)
    sea_like = cv2.morphologyEx(sea_like, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)); sea_like = cv2.morphologyEx(sea_like, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    _, labels = cv2.connectedComponents(sea_like, 8); selected = set()
    for x, y in sea_points:
        local = labels[max(0, y - 10):min(labels.shape[0], y + 11), max(0, x - 10):min(labels.shape[1], x + 11)]; ids, counts = np.unique(local[local > 0], return_counts=True)
        if len(ids): selected.add(int(ids[np.argmax(counts)]))
    connected = np.isin(labels, list(selected)).astype(np.uint8)
    return cv2.distanceTransform(connected, cv2.DIST_L2, 5) * factor >= float(config["sea_mask"]["deep_sea_safety_distance_px"])


def georeference(catalog: pd.DataFrame, roads: gpd.GeoDataFrame, controls_path: Path, map_id: str) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, np.ndarray]:
    _, coefficients, _ = fit_controls(controls_path)
    def transform_geometry(geometry):
        metric = M.affine_predict(np.asarray(geometry.coords), coefficients); lon, lat = M.TO_WGS84.transform(metric[:, 0], metric[:, 1]); return LineString(zip(lon, lat))
    geo_roads = roads.copy(); geo_roads.geometry = geo_roads.geometry.map(transform_geometry); geo_roads.set_crs("EPSG:4326", inplace=True); geo_roads["georeference_model"] = "AFFINE_FROM_AUTOMATIC_CROSSMAP_CONTROLS"; geo_roads["georeference_status"] = f"AUTOMATIC_INHERITED_NOT_HUMAN_VALIDATED_FOR_{map_id}"
    metric = M.affine_predict(catalog[["map_pixel_x", "map_pixel_y"]].to_numpy(float), coefficients); lon, lat = M.TO_WGS84.transform(metric[:, 0], metric[:, 1]); geo_localities = gpd.GeoDataFrame(catalog.copy(), geometry=[Point(x, y) for x, y in zip(lon, lat)], crs="EPSG:4326"); geo_localities["observed_map_lon"] = lon; geo_localities["observed_map_lat"] = lat
    return geo_roads, geo_localities, coefficients


def append_rasters(raster: Path, gpkg: Path, width: int, height: int, coefficients: np.ndarray, year: int, temporary_dir: Path) -> None:
    env = os.environ.copy(); env["JPEGMEM"] = "2048M"; env["GDAL_CACHEMAX"] = "1024"
    original = [str(M.locate_gdal_translate()), "-of", "GPKG", "-a_ullr", "0", str(height), str(width), "0", "-co", "APPEND_SUBDATASET=YES", "-co", f"RASTER_TABLE=carte_originale_{year}", "-co", "TILE_FORMAT=JPEG", "-co", "QUALITY=92", "-co", "BLOCKSIZE=512", str(raster), str(gpkg)]
    completed = subprocess.run(original, capture_output=True, text=True, env=env)
    if completed.returncode: raise RuntimeError(f"ECHEC_RASTER_ORIGINAL:{completed.stderr}")
    vrt = temporary_dir / f"carte_{year}_m0_aeqd.vrt"
    gt = [coefficients[0, 0], coefficients[1, 0], coefficients[2, 0], coefficients[0, 1], coefficients[1, 1], coefficients[2, 1]]
    translate = [str(M.locate_gdal_translate()), "-of", "VRT", "-a_srs", M.AEQD.to_proj4(), "-a_gt", *map(str, gt), str(raster), str(vrt)]
    completed = subprocess.run(translate, capture_output=True, text=True, env=env)
    if completed.returncode: raise RuntimeError(f"ECHEC_PREPARATION_GEOREFERENCE:{completed.stderr}")
    warp = [str(M.locate_gdal_warp()), "-overwrite", "-t_srs", "EPSG:4326", "-r", "bilinear", "-dstnodata", "255", "-of", "GPKG", "-co", "APPEND_SUBDATASET=YES", "-co", f"RASTER_TABLE=carte_{year}_georeferencee", "-co", "TILE_FORMAT=JPEG", "-co", "QUALITY=92", "-co", "BLOCKSIZE=512", str(vrt), str(gpkg)]
    completed = subprocess.run(warp, capture_output=True, text=True, env=env)
    if completed.returncode: raise RuntimeError(f"ECHEC_RASTER_GEOREFERENCE:{completed.stderr}")


def check(config_path: Path) -> dict:
    root = config_path.resolve().parent.parent; config = json.loads(config_path.read_text(encoding="utf-8")); year = int(config["map_year"])
    if config.get("map_id") != f"C{year}": raise ValueError("IDENTIFIANT_MILLESIME_INCOHERENT")
    paths = paths_for(root, config); width, height = Image.open(paths["raster"]).size; rules = json.loads(paths["road_rules"].read_text(encoding="utf-8"))
    if (width, height) != (int(config["width"]), int(config["height"])): raise ValueError("DIMENSIONS_RASTER_INCORRECTES")
    if M.sha256(paths["raster"]) != config["raster_sha256"].lower() or M.sha256(paths["raster"]) != rules["raster_sha256"].lower(): raise ValueError("EMPREINTE_RASTER_INCORRECTE")
    controls = pd.read_csv(paths["controls"])
    if len(controls) != 28: raise ValueError("CONTROLES_GEOREFERENCE_INCOMPLETS")
    return {"status": f"PASS_C{year}_C1938_ENGINE_ADAPTATION_PREFLIGHT", "pipeline_executed": False, "dimensions": [width, height], "control_count": len(controls), "engine": str(HERE / "001_pipeline_consolide_1938.py")}


def build(config_path: Path) -> dict:
    started_total = time.perf_counter(); root = config_path.resolve().parent.parent; config = json.loads(config_path.read_text(encoding="utf-8")); year = int(config["map_year"]); map_id = config["map_id"]; paths = paths_for(root, config); rules = json.loads(paths["road_rules"].read_text(encoding="utf-8")); configure_engine(config, rules); width, height = Image.open(paths["raster"]).size; rgb = np.asarray(Image.open(paths["raster"]).convert("RGB")); timings = {}
    def timed(name: str, begin: float, **extra): timings[name] = round(time.perf_counter() - begin, 3); print(json.dumps({"phase": name, "elapsed_seconds": timings[name], **extra}, ensure_ascii=False), flush=True)
    with tempfile.TemporaryDirectory(prefix=f"c{year}_depuis_1938_") as tmp_name:
        tmp = Path(tmp_name); begin = time.perf_counter(); raw = M.extract_roads(rgb, rules, tiles_from_rules(width, height, rules)); timed("routes_observees", begin, objects=len(raw))
        begin = time.perf_counter(); roads = M.assemble_roads(M.add_connectors(raw, rules["gap_rule"])); roads = rename_engine_ids(roads, map_id, year); timed("raccordements_et_topologie", begin, objects=len(roads) - len(raw))
        begin = time.perf_counter(); lines = M.run_ocr(rgb, paths["tesseract"], tmp); labels = M.semantic_labels(lines); labels["candidate_id"] = [f"{map_id}_OCR_{index:05d}" for index in range(1, len(labels) + 1)]; timed("ocr_et_classement", begin, lines=len(lines), labels=len(labels))
        begin = time.perf_counter(); circles = M.detect_circles(rgb, labels); circles["candidate_id"] = [f"{map_id}_LOCALITY_CIRCLE_{index:05d}" for index in range(1, len(circles) + 1)]; circles = attach_road_relations(circles, roads, width); timed("anneaux_et_relation_routiere", begin, circles=len(circles), effective=int(circles.effective_road_relation.sum()))
        begin = time.perf_counter(); catalog_all = automatic_catalog(circles, labels, paths["controls"], paths["geonames"], map_id); timed("association_localites", begin, objects=len(catalog_all))
        begin = time.perf_counter(); mask = sea_mask(rgb, paths["controls"], config); catalog = M.filter_sea(catalog_all, mask, int(config["sea_mask"]["downsample_factor"])); timed("exclusion_mer", begin, objects=len(catalog))
        begin = time.perf_counter(); geo_roads, geo_localities, coefficients = georeference(catalog, roads, paths["controls"], map_id); timed("calage_geographique", begin)
        map_roads = M.flip_for_map(roads, height); map_roads["origine_objet"] = map_roads.source_role.map({"V1_FRAGMENT": "FRAGMENT_OBSERVE", "PI_RULE_CONNECTOR": "RACCORDEMENT_COMPTE_COMME_ROUTE"}); map_roads["regle_humaine_appliquee"] = map_roads.source_role.eq("PI_RULE_CONNECTOR").astype(int)
        map_localities = gpd.GeoDataFrame(catalog.copy(), geometry=[Point(x, y) for x, y in zip(catalog.map_pixel_x, catalog.map_pixel_y)], crs=None); map_localities["validation_humaine"] = 0; map_localities = M.flip_for_map(map_localities, height); geo_localities["validation_humaine"] = 0; geo_roads["origine_objet"] = geo_roads.source_role.map({"V1_FRAGMENT": "FRAGMENT_OBSERVE", "PI_RULE_CONNECTOR": "RACCORDEMENT_COMPTE_COMME_ROUTE"}); geo_roads["regle_humaine_appliquee"] = geo_roads.source_role.eq("PI_RULE_CONNECTOR").astype(int)
        modern_roads = M.current_roads(paths["osm"]); modern_roads["geometry_role"] = f"CURRENT_REFERENCE_NEVER_DETERMINES_{year}_EXISTENCE"; modern_localities = gpd.GeoDataFrame(catalog.copy(), geometry=[Point(lon, lat) for lon, lat in zip(catalog.longitude, catalog.latitude)], crs="EPSG:4326"); modern_localities["scientific_status"] = f"CURRENT_POSITION_OF_RETAINED_HISTORICAL_LOCALITY_NEVER_DETERMINES_{year}_EXISTENCE"
        layers = {f"routes_{year}_carte": map_roads, f"localites_{year}_carte": map_localities, f"routes_{year}_geographiques": geo_roads, f"localites_{year}_geographiques": geo_localities, "routes_maroc_actuel": modern_roads, "localites_maroc_actuel": modern_localities}
        output = (root / config["output_geopackage"]).resolve(); output.parent.mkdir(parents=True, exist_ok=True); handle, temp_name = tempfile.mkstemp(prefix=f"c{year}_final_", suffix=".gpkg", dir=output.parent); os.close(handle); temporary = Path(temp_name); temporary.unlink()
        begin = time.perf_counter()
        try:
            for index, (layer, frame) in enumerate(layers.items()): frame.to_file(temporary, layer=layer, driver="GPKG", mode="w" if index == 0 else "a", engine="pyogrio")
            append_rasters(paths["raster"], temporary, width, height, coefficients, year, tmp)
            expected_tables = set(layers) | {f"carte_originale_{year}", f"carte_{year}_georeferencee"}
            if M.application_tables(temporary) != expected_tables: raise RuntimeError(f"TABLES_FINALES_INATTENDUES:{sorted(M.application_tables(temporary))}")
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        timed("ecriture_geopackage_et_rasters", begin, bytes=output.stat().st_size)
    roles = roads.source_role.value_counts().to_dict(); counts = {"road_fragments": int(roles.get("V1_FRAGMENT", 0)), "road_connectors": int(roles.get("PI_RULE_CONNECTOR", 0)), "roads": len(roads), "ocr_lines": len(lines), "place_labels": len(labels), "circle_candidates": len(circles), "circles_with_effective_road_relation": int(circles.effective_road_relation.sum()), "localities_before_sea_filter": len(catalog_all), "historical_localities": len(catalog), "human_validated_localities": 0, "modern_localities": len(modern_localities), "modern_roads": len(modern_roads)}; timings["total"] = round(time.perf_counter() - started_total, 3)
    result = {"status": f"C{year}_FULL_MAP_BUILT_FROM_C1938_ENGINE", "epistemic_status": "AUTOMATIC_FULL_MAP_CANDIDATE_NOT_HUMAN_VALIDATED", "output": str(output), "sha256": M.sha256(output), "counts": counts, "timings_seconds": timings, "engine": str(HERE / "001_pipeline_consolide_1938.py")}; output.with_suffix(".production.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"); print(json.dumps(result, ensure_ascii=False, indent=2)); return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--config", required=True, type=Path); mode = parser.add_mutually_exclusive_group(required=True); mode.add_argument("--check", action="store_true"); mode.add_argument("--run", action="store_true"); args = parser.parse_args(); result = check(args.config) if args.check else build(args.config)
    if args.check: print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
