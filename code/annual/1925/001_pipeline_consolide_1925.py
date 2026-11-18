#!/usr/bin/env python3
"""Pipeline consolidé C1925 : JPEG original vers GeoPackage comparé et visible."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import cv2
import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from PIL import Image, ImageEnhance
from pyproj import CRS, Transformer
from scipy.spatial import cKDTree
from shapely import affinity
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union
from skimage.morphology import remove_small_objects, skeletonize

Image.MAX_IMAGE_PIXELS = 300_000_000
FRAME = (450, 250, 16490, 15350)
LEGEND = (14200, 12150, 16650, 13600)
DISTANCE_TABLE = (11360, 12790, 16622, 15520)
TITLE_CORE = (2800, 500, 6100, 4200)
WORK_WIDTH = 6000
NEIGHBOUR_OFFSETS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
AEQD = CRS.from_proj4("+proj=aeqd +lat_0=32 +lon_0=-6 +datum=WGS84 +units=m +no_defs")
WGS84 = CRS.from_epsg(4326)
TO_METRES = Transformer.from_crs(WGS84, AEQD, always_xy=True)
TO_WGS84 = Transformer.from_crs(AEQD, WGS84, always_xy=True)
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().lower()


def normalise_name(value: object) -> str:
    plain = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z]+", "", plain)


def normalise_ocr(value: object) -> str:
    plain = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]+", " ", plain).strip()


def trace_skeleton(skeleton: np.ndarray, minimum_length: float, simplify: float) -> list[LineString]:
    ys, xs = np.nonzero(skeleton)
    pixels = set(zip(ys.tolist(), xs.tolist()))
    if not pixels:
        return []
    def neighbours(pixel):
        y, x = pixel
        return [(y + dy, x + dx) for dy, dx in NEIGHBOUR_OFFSETS if (y + dy, x + dx) in pixels]
    nodes = {pixel for pixel in pixels if len(neighbours(pixel)) != 2}
    visited = set()
    def edge(left, right): return (left, right) if left <= right else (right, left)
    def walk(start, following):
        coords = [start, following]; visited.add(edge(start, following)); previous, current = start, following
        while current not in nodes:
            choices = [item for item in neighbours(current) if item != previous]
            candidate = next((item for item in choices if edge(current, item) not in visited), None)
            if candidate is None: break
            visited.add(edge(current, candidate)); coords.append(candidate); previous, current = current, candidate
        return coords
    lines = []
    for start in list(nodes) + list(pixels):
        for following in neighbours(start):
            if edge(start, following) in visited: continue
            geometry = LineString([(x, y) for y, x in walk(start, following)]).simplify(simplify, preserve_topology=False)
            if geometry.length >= minimum_length: lines.append(geometry)
    return lines


def extract_roads(rgb_full: np.ndarray, rules: dict, tiles: list[dict]) -> gpd.GeoDataFrame:
    x_origins = sorted({int(tile["x0"]) for tile in tiles}); y_origins = sorted({int(tile["y0"]) for tile in tiles})
    full_height, full_width = rgb_full.shape[:2]; red_rule = rules["red_rule"]; excluded = list(rules["excluded_rectangles"].values()); records = []
    def ownership(tile):
        column, row = int(tile["grid_column"]) - 1, int(tile["grid_row"]) - 1
        x0, y0 = int(tile["x0"]), int(tile["y0"])
        return (0 if column == 0 else (x_origins[column-1]+x0+1024)//2,
                0 if row == 0 else (y_origins[row-1]+y0+1024)//2,
                full_width if column == len(x_origins)-1 else (x0+x_origins[column+1]+1024)//2,
                full_height if row == len(y_origins)-1 else (y0+y_origins[row+1]+1024)//2)
    for tile in sorted(tiles, key=lambda value: (int(value["grid_row"]), int(value["grid_column"]))):
        x0, y0, x1, y1 = [int(tile[key]) for key in ("x0", "y0", "x1", "y1")]
        crop = rgb_full[y0:y1, x0:x1]; hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV); red, green, blue = [crop[:, :, i].astype(np.int16) for i in range(3)]
        mask = ((hsv[:, :, 0] <= red_rule["hue_max"]) & (hsv[:, :, 1] > red_rule["saturation_min_exclusive"]) &
                (hsv[:, :, 2] > red_rule["value_min_exclusive"]) & (red > green + red_rule["red_minus_green_min_exclusive"]) &
                (red > blue + red_rule["red_minus_blue_min_exclusive"]))
        kernel = rules["morphological_close_kernel"]
        mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((kernel, kernel), np.uint8)).astype(bool)
        mask = remove_small_objects(mask, max_size=rules["remove_small_objects_max_size"])
        for left, top, right, bottom in excluded:
            if max(x0, left) < min(x1, right) and max(y0, top) < min(y1, bottom):
                mask[max(0, top-y0):min(crop.shape[0], bottom-y0), max(0, left-x0):min(crop.shape[1], right-x0)] = False
        own = ownership(tile); own_local = box(own[0]-x0, own[1]-y0, own[2]-x0, own[3]-y0)
        for line in trace_skeleton(skeletonize(mask), rules["trace_minimum_length"], rules["simplify_tolerance"]):
            clipped = line.intersection(own_local); parts = [clipped] if clipped.geom_type == "LineString" else list(getattr(clipped, "geoms", []))
            for part in parts:
                if part.geom_type == "LineString" and part.length >= rules["post_clip_minimum_length"]:
                    geometry = LineString([(x+x0, y+y0) for x, y in part.coords]); records.append({"source_role": "V1_FRAGMENT", "pixel_length": round(geometry.length, 3), "geometry": geometry})
    roads = gpd.GeoDataFrame(records, geometry="geometry", crs=None)
    roads.insert(0, "source_feature_id", [f"C1925_RV1_REPRO_{index:07d}" for index in range(1, len(roads)+1)])
    return roads


def add_connectors(roads: gpd.GeoDataFrame, rule: dict) -> gpd.GeoDataFrame:
    endpoints = []
    for index, geometry in enumerate(roads.geometry):
        coords = list(geometry.coords)
        for end in (0, -1):
            point = np.array(coords[end], float); neighbour = np.array(coords[1] if end == 0 else coords[-2], float); vector = point-neighbour; norm = np.linalg.norm(vector)
            if norm: endpoints.append((index, point, vector/norm))
    points = np.array([item[1] for item in endpoints]); tree = cKDTree(points); eligible = []; best = {}
    for left, right in tree.query_pairs(r=rule["maximum_distance_px"]):
        il, pl, vl = endpoints[left]; ir, pr, vr = endpoints[right]
        if il == ir: continue
        distance = float(np.linalg.norm(pr-pl))
        if distance < rule["minimum_distance_px"]: continue
        direction = (pr-pl)/distance
        if float(np.dot(vl, direction)) < rule["minimum_alignment_dot"] or float(np.dot(vr, -direction)) < rule["minimum_alignment_dot"]: continue
        eligible.append((distance, left, right, il, ir, pl, pr));
        if left not in best or distance < best[left][0]: best[left] = (distance, right)
        if right not in best or distance < best[right][0]: best[right] = (distance, left)
    rows, seen = [], set()
    for distance, left, right, il, ir, pl, pr in sorted(eligible):
        if best.get(left, (None, None))[1] != right or best.get(right, (None, None))[1] != left: continue
        pair = tuple(sorted((il, ir)))
        if pair in seen: continue
        seen.add(pair); rows.append({"source_role": "PI_RULE_CONNECTOR", "pixel_length": round(distance, 3), "geometry": LineString([tuple(pl), tuple(pr)])})
    gaps = gpd.GeoDataFrame(rows, geometry="geometry", crs=None)
    gaps.insert(0, "source_feature_id", [f"C1925_GAP_REPRO_{index:05d}" for index in range(1, len(gaps)+1)])
    return pd.concat([roads, gaps], ignore_index=True)


def assemble_roads(roads: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    node_keys = {}; rows = []
    def node_id(x, y):
        key = (round(x), round(y))
        if key not in node_keys: node_keys[key] = f"C1925_REPRO_NODE_{len(node_keys)+1:06d}"
        return node_keys[key]
    for index, row in enumerate(roads.itertuples(index=False), 1):
        geometry = row.geometry; rows.append({"edge_id": f"C1925_REPRO_OBS_{index:07d}", "source_feature_id": row.source_feature_id, "source_role": row.source_role, "map_year": 1925, "from_node": node_id(*geometry.coords[0]), "to_node": node_id(*geometry.coords[-1]), "network_class": "ROUTE_OR_TRACK_NOT_DISTINGUISHABLE_FROM_MAP", "operational_status": "BUILT_AND_UNDER_CONSTRUCTION_NOT_SEPARATED", "pixel_length": round(geometry.length, 3), "admissibility_status": "SOURCE_FIRST_REPRODUCTION", "source_method": "V1_RED_SOURCE_PIXELS_WITH_PI_APPROVED_GAP_CONNECTORS", "geometry": geometry})
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=None)


def run_ocr(rgb_full: np.ndarray, tesseract: Path, temporary: Path) -> pd.DataFrame:
    scale = min(1.0, WORK_WIDTH/rgb_full.shape[1]); resized = Image.fromarray(rgb_full)
    if scale < 1.0: resized = resized.resize((int(rgb_full.shape[1]*scale), int(rgb_full.shape[0]*scale)), Image.Resampling.LANCZOS)
    prepared = temporary/"ocr.png"; gray = resized.convert("L"); gray = ImageEnhance.Contrast(gray).enhance(1.6); ImageEnhance.Sharpness(gray).enhance(1.4).save(prepared)
    base = temporary/"ocr"; completed = subprocess.run([str(tesseract), str(prepared), str(base), "-l", "fra+frm", "--psm", "11", "tsv"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode: raise RuntimeError(f"ECHEC_TESSERACT:{completed.stderr}")
    words = pd.read_csv(base.with_suffix(".tsv"), sep="\t", dtype={"text": str}, quoting=csv.QUOTE_NONE, encoding_errors="replace", on_bad_lines="skip").fillna({"text": ""})
    words = words[(words.level == 5) & (words.text.str.strip() != "")]; rows = []; inverse = 1/scale
    for _, group in words.groupby(["page_num", "block_num", "par_num", "line_num"], sort=False):
        left, top = group.left.min(), group.top.min(); right, bottom = (group.left+group.width).max(), (group.top+group.height).max(); text = " ".join(group.text.astype(str))
        rows.append({"ocr_text": text, "ocr_normalized": normalise_ocr(text), "ocr_confidence": pd.to_numeric(group.conf, errors="coerce").mean(), "left": left*inverse, "top": top*inverse, "width": (right-left)*inverse, "height": (bottom-top)*inverse})
    return pd.DataFrame(rows)


def ocr_region(x: float, y: float) -> str:
    def inside(bounds): return bounds[0] <= x < bounds[2] and bounds[1] <= y < bounds[3]
    if inside(DISTANCE_TABLE): return "PRINCIPAL_DISTANCE_TABLE"
    if inside(LEGEND): return "LEGEND"
    if inside(TITLE_CORE): return "TITLE_CARTOUCHE"
    if inside(FRAME): return "MAP_USEFUL_FRAME"
    return "FRAME_OR_MARGIN"


def semantic_labels(lines: pd.DataFrame) -> pd.DataFrame:
    records = []
    for index, row in lines.iterrows():
        text = str(row.ocr_text).strip(); x = float(row.left+row.width/2); y = float(row.top+row.height/2); normalised = "" if pd.isna(row.ocr_normalized) or str(row.ocr_normalized) == "NA" else str(row.ocr_normalized); compact = re.sub(r"[^A-Z0-9]", "", normalised); region = ocr_region(x, y)
        if region == "PRINCIPAL_DISTANCE_TABLE": family = "DISTANCE_TABLE_OCR_CANDIDATE"
        elif re.fullmatch(r"R[PS]\d*", compact) or re.search(r"\bR\.?[PS]\.?\b", normalised): family = "ROAD_NUMBER_CANDIDATE"
        elif re.fullmatch(r"\d{1,4}", compact): family = "MAP_DISTANCE_OR_GRID_NUMBER_CANDIDATE"
        elif compact in {"T", "I"}: family = "FACILITY_SYMBOL_T_OR_I_CANDIDATE"
        elif sum(char.isalpha() for char in text) >= 2: family = "TOPONYM_OR_CARTOGRAPHIC_LABEL_CANDIDATE"
        else: family = "OTHER_OCR_CANDIDATE"
        records.append({"candidate_id": f"C1925_OCR_{index+1:05d}", "ocr_text": text, "ocr_normalized": normalised, "ocr_confidence": float(row.ocr_confidence), "center_x": x, "center_y": y, "map_region": region, "candidate_family": family})
    inventory = pd.DataFrame(records)
    return inventory[(inventory.candidate_family == "TOPONYM_OR_CARTOGRAPHIC_LABEL_CANDIDATE") & (inventory.map_region == "MAP_USEFUL_FRAME")].copy()


def circle_masks(rgb: np.ndarray):
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV); gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY); red, green, blue = [rgb[:, :, i].astype(np.int16) for i in range(3)]
    red_mask = ((red > 125) & (red > green+22) & (red > blue+28) & (hsv[:, :, 1] > 55)).astype(np.uint8); dark = ((gray < 115) & (np.maximum.reduce([red, green, blue])-np.minimum.reduce([red, green, blue]) < 65)).astype(np.uint8)
    return gray, red_mask, dark


def circle_features(cx, cy, radius, red, dark):
    pad = int(max(30, radius*4)); x0, x1 = max(0, int(cx-pad)), min(red.shape[1], int(cx+pad+1)); y0, y1 = max(0, int(cy-pad)), min(red.shape[0], int(cy+pad+1)); yy, xx = np.mgrid[y0:y1, x0:x1]; distance = np.sqrt((xx-cx)**2+(yy-cy)**2); angle = (np.arctan2(yy-cy, xx-cx)+2*np.pi)%(2*np.pi)
    ring = (distance >= radius*.68) & (distance <= radius*1.35); inner = distance <= radius*.62; nearby = distance <= radius*2.8; core = distance <= radius*1.55; red_annulus = (distance >= radius*.8) & (distance <= radius*3) & (red[y0:y1, x0:x1] > 0); dark_ring = ring & (dark[y0:y1, x0:x1] > 0)
    return (float(dark[y0:y1, x0:x1][ring].mean()) if ring.any() else 0., float(dark[y0:y1, x0:x1][inner].mean()) if inner.any() else 0., int(red[y0:y1, x0:x1][nearby].sum()), int(red[y0:y1, x0:x1][core].sum()), len(np.unique((angle[red_annulus]/(2*np.pi)*16).astype(int))) if red_annulus.any() else 0, len(np.unique((angle[dark_ring]/(2*np.pi)*36).astype(int))) if dark_ring.any() else 0)


def detect_circles(rgb_full: np.ndarray, labels: pd.DataFrame) -> pd.DataFrame:
    full_height, full_width = rgb_full.shape[:2]; work_height = round(full_height*WORK_WIDTH/full_width); rgb = np.asarray(Image.fromarray(rgb_full).resize((WORK_WIDTH, work_height), Image.Resampling.LANCZOS)); gray, red, dark = circle_masks(rgb); sx, sy = full_width/WORK_WIDTH, full_height/work_height; wx0, wy0, wx1, wy1 = FRAME[0]/sx, FRAME[1]/sy, FRAME[2]/sx, FRAME[3]/sy; rows = []
    for y0 in range(0, work_height, 800):
        for x0 in range(0, WORK_WIDTH, 800):
            crop = cv2.GaussianBlur(gray[y0:min(work_height, y0+900), x0:min(WORK_WIDTH, x0+900)], (5, 5), 1.1); circles = cv2.HoughCircles(crop, cv2.HOUGH_GRADIENT, dp=1.15, minDist=11, param1=95, param2=14, minRadius=5, maxRadius=15)
            if circles is None: continue
            for local_x, local_y, radius in circles[0]:
                cx, cy, radius = float(local_x+x0), float(local_y+y0), float(radius)
                if not (wx0 <= cx <= wx1 and wy0 <= cy <= wy1): continue
                ring_dark, inner_dark, nearby_red, core_red, red_sectors, dark_sectors = circle_features(cx, cy, radius, red, dark)
                if nearby_red < 8 or red_sectors < 1 or ring_dark < .08: continue
                rows.append({"work_x": cx, "work_y": cy, "work_radius": radius, "ring_dark_fraction": ring_dark, "inner_dark_fraction": inner_dark, "nearby_red_pixels": nearby_red, "core_red_pixels": core_red, "red_angular_sectors": red_sectors, "dark_ring_angular_sectors_36": dark_sectors, "raw_score": 3.2*ring_dark+min(nearby_red, 140)/140+min(red_sectors, 8)/8+min(core_red, 50)/80})
    kept = []
    for row in sorted(rows, key=lambda item: item["raw_score"], reverse=True):
        if all((row["work_x"]-other["work_x"])**2+(row["work_y"]-other["work_y"])**2 > 49 for other in kept): kept.append(row)
    candidates = pd.DataFrame(kept); candidates["pixel_x"] = candidates.work_x*sx; candidates["pixel_y"] = candidates.work_y*sy; distance, indices = cKDTree(labels[["center_x", "center_y"]].to_numpy(float)).query(candidates[["pixel_x", "pixel_y"]].to_numpy(float)); candidates["nearest_label_distance_px"] = distance; candidates["nearest_ocr_text"] = [labels.iloc[index].ocr_text for index in indices]; candidates["candidate_id"] = [f"C1925_LOCALITY_CIRCLE_{index:05d}" for index in range(1, len(candidates)+1)]
    return candidates


def attach_road_relations(circles: pd.DataFrame, roads: gpd.GeoDataFrame, source_width: int) -> pd.DataFrame:
    """Relier chaque anneau au réseau observé et aux raccordements, qui comptent comme routes."""
    result = circles.copy()
    observed = unary_union(roads.loc[roads.source_role.eq("V1_FRAGMENT"), "geometry"])
    connectors = unary_union(roads.loc[roads.source_role.eq("PI_RULE_CONNECTOR"), "geometry"])
    points = [Point(x, y) for x, y in zip(result.pixel_x, result.pixel_y)]
    result["circle_radius_source_px"] = result.work_radius * source_width / WORK_WIDTH
    result["observed_road_distance_px"] = [point.distance(observed) for point in points]
    result["connector_distance_px"] = [point.distance(connectors) for point in points]
    observed_relation = result.observed_road_distance_px <= 2.0 * result.circle_radius_source_px + 3.0
    connector_relation = result.connector_distance_px <= result.circle_radius_source_px + 3.0
    result["effective_road_relation"] = observed_relation | connector_relation
    result["road_relation_type"] = np.select(
        [connector_relation, observed_relation],
        ["CONNECTOR_COUNTS_AS_ROAD", "OBSERVED_ROAD_TOUCHES_CIRCLE"],
        default="NO_EFFECTIVE_ROAD_RELATION",
    )
    return result


def affine_fit(x, y): return np.linalg.lstsq(np.c_[np.ones(len(x)), x], y, rcond=None)[0]
def affine_predict(x, coefficients): return np.c_[np.ones(len(x)), x]@coefficients


def read_geonames(path: Path) -> pd.DataFrame:
    columns = ["geonameid", "name", "asciiname", "alternatenames", "latitude", "longitude", "feature_class", "feature_code", "country_code", "cc2", "admin1", "admin2", "admin3", "admin4", "population", "elevation", "dem", "timezone", "modification_date"]
    frame = pd.read_csv(path, sep="\t", names=columns, low_memory=False)
    return frame[frame.feature_class.eq("P")].copy()


def aliases(row) -> list[str]:
    values = [row["name"], row["asciiname"], *str(row.get("alternatenames", "")).split(",")[:80]]
    return [normalise_name(value) for value in values if len(normalise_name(value)) >= 3]


def similarity(left: str, right: str) -> float:
    if not left or not right: return 0.
    base = SequenceMatcher(None, left, right).ratio(); partial = SequenceMatcher(None, left, right[:len(left)]).ratio() if len(right) >= len(left) else SequenceMatcher(None, left[:len(right)], right).ratio()
    return max(base, partial)


def build_locality_catalog(circles: pd.DataFrame, labels: pd.DataFrame, human_path: Path, m0_path: Path, geonames_path: Path) -> pd.DataFrame:
    human = pd.read_csv(human_path); controls = human.merge(pd.read_csv(m0_path), on="city_id", validate="one_to_one"); pixels = controls[["map_pixel_x_candidate", "map_pixel_y_candidate"]].to_numpy(float); east, north = TO_METRES.transform(controls.lon.to_numpy(), controls.lat.to_numpy()); metric = np.c_[east, north]; forward = affine_fit(pixels, metric); inverse = affine_fit(metric, pixels)
    gazetteer = read_geonames(geonames_path); east, north = TO_METRES.transform(gazetteer.longitude.to_numpy(), gazetteer.latitude.to_numpy()); gazetteer["east"] = east; gazetteer["north"] = north; gazetteer_tree = cKDTree(gazetteer[["east", "north"]].to_numpy(float)); alias_cache = [aliases(row) for _, row in gazetteer.iterrows()]
    ocr = labels.dropna(subset=["center_x", "center_y", "ocr_text"]).copy(); ocr["ocr_norm"] = ocr.ocr_text.map(normalise_name); ocr = ocr[ocr.ocr_norm.str.len() >= 3].copy(); label_metric = affine_predict(ocr[["center_x", "center_y"]].to_numpy(float), forward)
    eligible = circles[(circles.dark_ring_angular_sectors_36 >= 16) & (circles.nearest_label_distance_px <= 550) & circles.effective_road_relation].copy(); circle_tree = cKDTree(eligible[["pixel_x", "pixel_y"]].to_numpy(float)); proposals = []
    for position, (_, label) in enumerate(ocr.iterrows()):
        nearby = gazetteer_tree.query_ball_point(label_metric[position], r=30000); scored = []
        for gazetteer_index in nearby:
            name_score = max((similarity(label.ocr_norm, alias) for alias in alias_cache[gazetteer_index]), default=0.)
            if name_score < .52: continue
            geo_distance = np.linalg.norm(gazetteer.iloc[gazetteer_index][["east", "north"]].to_numpy(float)-label_metric[position])/1000
            scored.append((name_score-.006*geo_distance, name_score, geo_distance, gazetteer_index))
        if not scored: continue
        _, name_score, geo_distance, gazetteer_index = max(scored); gazetteer_row = gazetteer.iloc[gazetteer_index]; expected = affine_predict(np.array([[gazetteer_row.east, gazetteer_row.north]]), inverse)[0]; candidates = circle_tree.query_ball_point(expected, r=280); candidates = [index for index in candidates if np.linalg.norm(eligible.iloc[index][["pixel_x", "pixel_y"]].to_numpy(float)-label[["center_x", "center_y"]].to_numpy(float)) <= 550]
        if not candidates: continue
        best = min(candidates, key=lambda index: np.linalg.norm(eligible.iloc[index][["pixel_x", "pixel_y"]].to_numpy(float)-expected)-18*eligible.iloc[index].raw_score); circle = eligible.iloc[best]; pixel_distance = float(np.linalg.norm(circle[["pixel_x", "pixel_y"]].to_numpy(float)-expected)); label_distance = float(np.linalg.norm(circle[["pixel_x", "pixel_y"]].to_numpy(float)-label[["center_x", "center_y"]].to_numpy(float)))
        if name_score >= .72 and geo_distance <= 15 and pixel_distance <= 150: tier = "HIGH_AUTOMATIC"
        elif name_score >= .55 and geo_distance <= 30 and pixel_distance <= 280: tier = "MEDIUM_AUTOMATIC"
        else: continue
        proposals.append({"circle_candidate_id": circle.candidate_id, "label_candidate_id": label.candidate_id, "historical_ocr_text": label.ocr_text, "geonameid": int(gazetteer_row.geonameid), "gazetteer_name": gazetteer_row["name"], "gazetteer_ascii_name": gazetteer_row.asciiname, "gazetteer_feature_code": gazetteer_row.feature_code, "gazetteer_population": int(gazetteer_row.population) if pd.notna(gazetteer_row.population) else 0, "longitude": gazetteer_row.longitude, "latitude": gazetteer_row.latitude, "map_pixel_x": circle.pixel_x, "map_pixel_y": circle.pixel_y, "name_similarity": name_score, "label_geographic_distance_km": geo_distance, "circle_to_expected_pixel_distance": pixel_distance, "circle_to_label_distance_px": label_distance, "circle_radius_source_px": circle.circle_radius_source_px, "observed_road_distance_px": circle.observed_road_distance_px, "connector_distance_px": circle.connector_distance_px, "effective_road_relation": True, "road_relation_type": circle.road_relation_type, "automatic_tier": tier, "status": "AUTOMATIC_MAP_CIRCLE_PLUS_MAP_LABEL_PLUS_GAZETTEER_PLUS_EFFECTIVE_ROAD_RELATION", "rank_score": name_score*6-geo_distance/35-pixel_distance/240+np.log1p(max(0, gazetteer_row.population))*.12})
    automatic = pd.DataFrame(proposals).sort_values("rank_score", ascending=False).drop_duplicates("circle_candidate_id").drop_duplicates("geonameid").drop_duplicates("label_candidate_id")
    keep = []
    for row in automatic.itertuples():
        duplicate = False; candidate_name = normalise_name(row.gazetteer_name)
        for control in controls.itertuples():
            names = {normalise_name(control.historical_name), normalise_name(control.modern_name)}; same = any(candidate_name == name or (len(candidate_name) >= 5 and len(name) >= 5 and SequenceMatcher(None, candidate_name, name).ratio() >= .88) for name in names); close = np.hypot(row.map_pixel_x-control.map_pixel_x_candidate, row.map_pixel_y-control.map_pixel_y_candidate) <= 300
            if same and close: duplicate = True; break
        keep.append(not duplicate)
    automatic = automatic.loc[keep].copy(); locked = []
    for row in controls.itertuples():
        locked.append({"circle_candidate_id": f"VALIDATED_{row.order:02d}", "label_candidate_id": "PI_VALIDATED", "historical_ocr_text": row.historical_name, "geonameid": None, "gazetteer_name": row.modern_name, "gazetteer_ascii_name": row.modern_name, "gazetteer_feature_code": "CONTROL", "gazetteer_population": None, "longitude": row.lon, "latitude": row.lat, "map_pixel_x": row.map_pixel_x_candidate, "map_pixel_y": row.map_pixel_y_candidate, "name_similarity": 1., "label_geographic_distance_km": 0., "circle_to_expected_pixel_distance": 0., "circle_to_label_distance_px": 0., "circle_radius_source_px": None, "observed_road_distance_px": None, "connector_distance_px": None, "effective_road_relation": True, "road_relation_type": "HUMAN_VALIDATED_CIRCLE_ON_ROAD", "automatic_tier": "HUMAN_VALIDATED_CONTROL", "status": "HUMAN_VALIDATED_BY_FAROUK", "rank_score": 999.})
    catalog = pd.concat([pd.DataFrame(locked), automatic], ignore_index=True); catalog["locality_id"] = [f"C1925_LOCALITY_{index:04d}" for index in range(1, len(catalog)+1)]
    return catalog


def deep_sea_mask(rgb_full: np.ndarray, catalog: pd.DataFrame, config: dict) -> np.ndarray:
    factor = int(config["sea_mask"]["downsample_factor"]); small = cv2.resize(cv2.cvtColor(rgb_full, cv2.COLOR_RGB2BGR), ((rgb_full.shape[1]+factor-1)//factor, (rgb_full.shape[0]+factor-1)//factor), interpolation=cv2.INTER_AREA).astype(np.float32); sea_samples = []; sea_points = []
    for x0, y0, x1, y1 in config["sea_mask"]["seed_boxes"]:
        x, y = (x0+x1)/2/factor, (y0+y1)/2/factor; sea_points.append((int(round(x)), int(round(y)))); yy, xx = np.ogrid[:small.shape[0], :small.shape[1]]; ring = ((xx-x)**2+(yy-y)**2 >= 9) & ((xx-x)**2+(yy-y)**2 <= 64); sea_samples.append(small[ring])
    land_samples = []
    for row in catalog[catalog.status.eq("HUMAN_VALIDATED_BY_FAROUK")].itertuples():
        x, y = row.map_pixel_x/factor, row.map_pixel_y/factor; yy, xx = np.ogrid[:small.shape[0], :small.shape[1]]; ring = ((xx-x)**2+(yy-y)**2 >= 9) & ((xx-x)**2+(yy-y)**2 <= 64); land_samples.append(small[ring])
    threshold = float((np.median(np.concatenate(sea_samples)[:, 1]-np.concatenate(sea_samples)[:, 2])+np.median(np.concatenate(land_samples)[:, 1]-np.concatenate(land_samples)[:, 2]))/2); sea_like = ((small[:, :, 1]-small[:, :, 2]) >= threshold).astype(np.uint8); sea_like = cv2.morphologyEx(sea_like, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)); sea_like = cv2.morphologyEx(sea_like, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)); _, labels = cv2.connectedComponents(sea_like, 8); selected = set()
    for x, y in sea_points:
        local = labels[max(0, y-10):min(labels.shape[0], y+11), max(0, x-10):min(labels.shape[1], x+11)]; ids, counts = np.unique(local[local > 0], return_counts=True)
        if len(ids): selected.add(int(ids[np.argmax(counts)]))
    fx0, fy0, fx1, fy1 = [int(round(value/factor)) for value in FRAME]; depth = max(1, int(round(config["sea_mask"]["border_seed_search_depth_px"]/factor))); border = np.concatenate((labels[fy0:min(fy1, fy0+depth), fx0:fx1+1].ravel(), labels[max(fy0, fy1-depth):fy1+1, fx0:fx1+1].ravel(), labels[fy0:fy1+1, fx0:min(fx1, fx0+depth)].ravel(), labels[fy0:fy1+1, max(fx0, fx1-depth):fx1+1].ravel())); selected.update(int(value) for value in np.unique(border) if value > 0); connected = np.isin(labels, list(selected)).astype(np.uint8); distance = cv2.distanceTransform(connected, cv2.DIST_L2, 5)*factor
    return distance >= float(config["sea_mask"]["deep_sea_safety_distance_px"])


def filter_sea(catalog: pd.DataFrame, mask: np.ndarray, factor: int) -> pd.DataFrame:
    keep = []
    for row in catalog.itertuples():
        x = min(mask.shape[1]-1, max(0, int(row.map_pixel_x/factor))); y = min(mask.shape[0]-1, max(0, int(row.map_pixel_y/factor))); keep.append(not bool(mask[y, x]))
    return catalog.loc[keep].copy().reset_index(drop=True)


def m0_georeference(catalog: pd.DataFrame, roads: gpd.GeoDataFrame, human_path: Path, m0_path: Path):
    controls = pd.read_csv(human_path).merge(pd.read_csv(m0_path), on="city_id", validate="one_to_one"); pixels = controls[["map_pixel_x_candidate", "map_pixel_y_candidate"]].to_numpy(float); east, north = TO_METRES.transform(controls.lon.to_numpy(), controls.lat.to_numpy()); coefficients = affine_fit(pixels, np.c_[east, north])
    def transform_geometry(geometry):
        metric = affine_predict(np.asarray(geometry.coords), coefficients); lon, lat = TO_WGS84.transform(metric[:, 0], metric[:, 1]); return LineString(zip(lon, lat))
    geo_roads = roads.copy(); geo_roads.geometry = geo_roads.geometry.map(transform_geometry); geo_roads.set_crs("EPSG:4326", inplace=True); geo_roads["georeference_model"] = "M0_CITY_AFFINE_VALIDATED28"; geo_roads["georeference_status"] = "CANONICAL_AFTER_M1_FAILED_10PCT_GATE"
    observed_metric = affine_predict(catalog[["map_pixel_x", "map_pixel_y"]].to_numpy(float), coefficients); lon, lat = TO_WGS84.transform(observed_metric[:, 0], observed_metric[:, 1]); observed = np.c_[lon, lat]; geo_localities = gpd.GeoDataFrame(catalog.copy(), geometry=[Point(x, y) for x, y in observed], crs="EPSG:4326"); geo_localities["observed_map_lon"] = observed[:, 0]; geo_localities["observed_map_lat"] = observed[:, 1]
    return geo_roads, geo_localities, coefficients


def current_roads(path: Path) -> gpd.GeoDataFrame:
    classes = ("motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified", "track")
    where = "highway IN ("+",".join(f"'{value}'" for value in classes)+")"; frame = pyogrio.read_dataframe(path, layer="lines", where=where, columns=["osm_id", "name", "highway", "z_order"]); frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty].copy().explode(index_parts=False, ignore_index=True); frame["current_edge_id"] = [f"OSM_{value}_{index}" for index, value in enumerate(frame.osm_id.astype(str), 1)]; frame["source_snapshot_sha256"] = sha256(path); frame["geometry_role"] = "CURRENT_REFERENCE_NEVER_DETERMINES_1925_EXISTENCE"; frame["review_status"] = "CURRENT_REFERENCE_NOT_HISTORICAL_EVIDENCE"
    frame["source_snapshot_sha256"] = frame["source_snapshot_sha256"].str.upper(); frame["review_status"] = "CURRENT_REFERENCE_AUTOMATIC_NOT_HISTORICAL_EVIDENCE"
    if frame.crs is None: frame.set_crs("EPSG:4326", inplace=True)
    return frame


def flip_for_map(frame: gpd.GeoDataFrame, height: int) -> gpd.GeoDataFrame:
    result = frame.copy(); result.geometry = result.geometry.map(lambda geometry: affinity.affine_transform(geometry, [1, 0, 0, -1, 0, float(height)])); result.set_crs(None, allow_override=True, inplace=True); result["espace_geometrique"] = "CARTE_ORIGINALE_X_DROITE_Y_HAUT"; return result


def locate_gdal_translate() -> Path:
    found = shutil.which("gdal_translate")
    if found: return Path(found)
    candidates = sorted(Path("C:/Program Files").glob("QGIS */**/gdal_translate.exe"), reverse=True)
    if not candidates: raise FileNotFoundError("GDAL_TRANSLATE_INTROUVABLE")
    return candidates[0]


def locate_gdal_warp() -> Path:
    candidate = locate_gdal_translate().with_name("gdalwarp.exe")
    if candidate.is_file(): return candidate
    found = shutil.which("gdalwarp")
    if found: return Path(found)
    raise FileNotFoundError("GDAL_WARP_INTROUVABLE")


def append_original_raster(raster: Path, geopackage: Path, width: int, height: int) -> None:
    """Embarquer la preuve documentaire sans rotation ni rééchantillonnage."""
    environment = os.environ.copy(); environment["JPEGMEM"] = "2048M"; environment["GDAL_CACHEMAX"] = "1024"
    command = [
        str(locate_gdal_translate()), "-of", "GPKG", "-a_ullr", "0", str(height), str(width), "0",
        "-co", "APPEND_SUBDATASET=YES", "-co", "RASTER_TABLE=carte_originale_1925",
        "-co", "TILE_FORMAT=JPEG", "-co", "QUALITY=92", "-co", "BLOCKSIZE=512",
        str(raster), str(geopackage),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, env=environment)
    if completed.returncode: raise RuntimeError(f"ECHEC_INTEGRATION_RASTER_ORIGINAL:{completed.stderr}")


def append_georeferenced_raster(raster: Path, geopackage: Path, coefficients: np.ndarray, temporary_dir: Path) -> None:
    """Publier le JPEG dans le même espace M0 que les objets historiques."""
    vrt = temporary_dir / "carte_1925_m0_aeqd.vrt"
    # affine_fit renvoie [constante, coefficient_x, coefficient_y] pour Est et Nord.
    geotransform = [
        coefficients[0, 0], coefficients[1, 0], coefficients[2, 0],
        coefficients[0, 1], coefficients[1, 1], coefficients[2, 1],
    ]
    environment = os.environ.copy(); environment["JPEGMEM"] = "2048M"; environment["GDAL_CACHEMAX"] = "1024"
    translate = [str(locate_gdal_translate()), "-of", "VRT", "-a_srs", AEQD.to_proj4(), "-a_gt", *map(str, geotransform), str(raster), str(vrt)]
    completed = subprocess.run(translate, capture_output=True, text=True, env=environment)
    if completed.returncode: raise RuntimeError(f"ECHEC_PREPARATION_RASTER_M0:{completed.stderr}")
    warp = [
        str(locate_gdal_warp()), "-overwrite", "-t_srs", "EPSG:4326", "-r", "bilinear",
        "-dstnodata", "255", "-of", "GPKG", "-co", "APPEND_SUBDATASET=YES",
        "-co", "RASTER_TABLE=carte_1925_georeferencee", "-co", "TILE_FORMAT=JPEG",
        "-co", "QUALITY=92", "-co", "BLOCKSIZE=512", str(vrt), str(geopackage),
    ]
    completed = subprocess.run(warp, capture_output=True, text=True, env=environment)
    if completed.returncode: raise RuntimeError(f"ECHEC_INTEGRATION_RASTER_GEOREFERENCE:{completed.stderr}")


def application_tables(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try: return {row[0] for row in connection.execute("SELECT table_name FROM gpkg_contents")}
    finally: connection.close()


def record_timing(timings: dict[str, float], phase: str, started: float, **details) -> None:
    elapsed = round(time.perf_counter()-started, 3); timings[phase] = elapsed
    print(json.dumps({"phase": phase, "elapsed_seconds": elapsed, **details}, ensure_ascii=False), flush=True)


def tiles_from_rules(width: int, height: int, rules: dict) -> list[dict]:
    size = int(rules["tiling"]["tile_size"]); stride = int(rules["tiling"]["stride"])
    xs = list(range(0, max(1, width-size+1), stride)); ys = list(range(0, max(1, height-size+1), stride))
    if not xs or xs[-1] != max(0, width-size): xs.append(max(0, width-size))
    if not ys or ys[-1] != max(0, height-size): ys.append(max(0, height-size))
    rows = []
    for row, y0 in enumerate(sorted(set(ys)), 1):
        for column, x0 in enumerate(sorted(set(xs)), 1):
            rows.append({"tile_id": f"C1925_T_R{row:02d}_C{column:02d}", "grid_row": row, "grid_column": column, "x0": x0, "y0": y0, "x1": min(width, x0+size), "y1": min(height, y0+size)})
    return rows


def normalised_human_decisions(decisions_path: Path, controls_path: Path, target: Path) -> pd.DataFrame:
    decisions = pd.read_csv(decisions_path).rename(columns={"functional_unit_id_main": "city_id", "city": "modern_name", "pi_clicked_x_source_px": "map_pixel_x_candidate", "pi_clicked_y_source_down_px": "map_pixel_y_candidate"})
    decisions["historical_name"] = decisions["modern_name"]
    decisions["order"] = np.arange(1, len(decisions)+1)
    decisions["status"] = "HUMAN_VALIDATED_BY_FAROUK"
    decisions["selection_rule"] = "PI_VALIDATED_CITY_CIRCLE_CLICK"
    decisions[["order", "city_id", "historical_name", "modern_name", "map_pixel_x_candidate", "map_pixel_y_candidate", "selection_rule", "status"]].to_csv(target, index=False)
    controls = pd.read_csv(controls_path)
    if set(decisions.city_id) != set(controls.city_id): raise RuntimeError("CONTROLES_1925_NON_ALIGNES_28")
    return decisions


def build_product_1925(config_path: Path) -> dict:
    global FRAME, LEGEND, DISTANCE_TABLE, TITLE_CORE, WORK_WIDTH
    total_started = time.perf_counter(); timings = {}; root = config_path.resolve().parent.parent; config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("map_id") != "C1925" or int(config.get("map_year")) != 1925: raise ValueError("CONFIGURATION_1925_REQUISE")
    raster = (root/config["raster"]).resolve(strict=True); rules_path = (root/config["road_extraction_config"]).resolve(strict=True); decisions_path = (root/config["human_locality_decisions"]).resolve(strict=True); controls_path = (root/config["m0_control_coordinates"]).resolve(strict=True); geonames = (root/config["geonames"]).resolve(strict=True); osm = (root/config["osm_pbf"]).resolve(strict=True); tesseract = (root/config["tesseract_executable"]).resolve(strict=True)
    rules = json.loads(rules_path.read_text(encoding="utf-8")); width, height = Image.open(raster).size
    if (width, height) != (int(config["width"]), int(config["height"])) or sha256(raster) != config["raster_sha256"].lower(): raise RuntimeError("RASTER_1925_INCORRECT")
    FRAME = tuple(rules["frame"]); TITLE_CORE = tuple(rules["excluded_rectangles"]["title_and_legend"]); LEGEND = TITLE_CORE; DISTANCE_TABLE = (-1, -1, -1, -1); WORK_WIDTH = min(6000, width)
    tiles = tiles_from_rules(width, height, rules); rgb = np.asarray(Image.open(raster).convert("RGB")); record_timing(timings, "lecture_entrees_et_raster", total_started, tiles=len(tiles))
    with tempfile.TemporaryDirectory(prefix="c1925_pipeline_") as temporary_name:
        temporary_dir = Path(temporary_name); normalized_human = temporary_dir/"human_1925_normalized.csv"; human = normalised_human_decisions(decisions_path, controls_path, normalized_human)
        started = time.perf_counter(); raw_roads = extract_roads(rgb, rules, tiles); record_timing(timings, "routes_observees", started, objects=len(raw_roads))
        started = time.perf_counter(); roads = assemble_roads(add_connectors(raw_roads, rules["gap_rule"])); roads["admissibility_status"] = "C1925_SOURCE_FIRST_CANDIDATE_PENDING_REFERENCE_COMPARISON"; record_timing(timings, "raccordements_et_topologie", started, objects=len(roads)-len(raw_roads))
        started = time.perf_counter(); lines = run_ocr(rgb, tesseract, temporary_dir); labels = semantic_labels(lines); record_timing(timings, "ocr_et_classement", started, lines=len(lines), labels=len(labels))
        started = time.perf_counter(); circles = attach_road_relations(detect_circles(rgb, labels), roads, width); record_timing(timings, "detection_anneaux_et_relation_routiere", started, objects=len(circles), effective_relations=int(circles.effective_road_relation.sum()))
        started = time.perf_counter(); catalog_all = build_locality_catalog(circles, labels, normalized_human, controls_path, geonames); catalog_all["locality_id"] = [f"C1925_LOCALITY_{index:04d}" for index in range(1, len(catalog_all)+1)]; record_timing(timings, "association_localites", started, objects=len(catalog_all))
        started = time.perf_counter(); mask = deep_sea_mask(rgb, catalog_all, config); catalog = filter_sea(catalog_all, mask, int(config["sea_mask"]["downsample_factor"])); record_timing(timings, "exclusion_mer", started, objects=len(catalog))
        started = time.perf_counter(); geo_roads, geo_localities, coefficients = m0_georeference(catalog, roads, normalized_human, controls_path); geo_roads["georeference_status"] = "C1925_SOURCE_FIRST_CANDIDATE"; record_timing(timings, "calage_m0", started)
        map_localities = gpd.GeoDataFrame(catalog.copy(), geometry=[Point(x, y) for x, y in zip(catalog.map_pixel_x, catalog.map_pixel_y)], crs=None); map_localities["validation_humaine"] = map_localities.automatic_tier.eq("HUMAN_VALIDATED_CONTROL").astype(int); map_localities = flip_for_map(map_localities, height); map_roads = flip_for_map(roads, height); map_roads["origine_objet"] = map_roads.source_role.map({"V1_FRAGMENT": "FRAGMENT_OBSERVE", "PI_RULE_CONNECTOR": "RACCORDEMENT_COMPTE_COMME_ROUTE"}); map_roads["regle_humaine_appliquee"] = map_roads.source_role.eq("PI_RULE_CONNECTOR").astype(int); geo_localities["validation_humaine"] = geo_localities.automatic_tier.eq("HUMAN_VALIDATED_CONTROL").astype(int); geo_roads["origine_objet"] = geo_roads.source_role.map({"V1_FRAGMENT": "FRAGMENT_OBSERVE", "PI_RULE_CONNECTOR": "RACCORDEMENT_COMPTE_COMME_ROUTE"}); geo_roads["regle_humaine_appliquee"] = geo_roads.source_role.eq("PI_RULE_CONNECTOR").astype(int)
        article_map = map_localities[map_localities.validation_humaine.eq(1)].copy(); article_geo = geo_localities[geo_localities.validation_humaine.eq(1)].copy(); modern_roads = current_roads(osm); modern_localities = gpd.GeoDataFrame(catalog.copy(), geometry=[Point(lon, lat) for lon, lat in zip(catalog.longitude, catalog.latitude)], crs="EPSG:4326"); modern_localities["ville_article_core28"] = modern_localities.automatic_tier.eq("HUMAN_VALIDATED_CONTROL").astype(int); modern_localities["scientific_status"] = "CURRENT_POSITION_OF_RETAINED_HISTORICAL_LOCALITY_NEVER_DETERMINES_1925_EXISTENCE"
        layers = {"routes_1925_carte": map_roads, "localites_1925_carte": map_localities, "villes_article_1925_carte": article_map, "routes_1925_geographiques": geo_roads, "localites_1925_geographiques": geo_localities, "villes_article_1925_geographiques": article_geo, "routes_maroc_actuel": modern_roads, "localites_maroc_actuel": modern_localities}
        output = (root/config["output_geopackage"]).resolve(); output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="c1925_source_first_", suffix=".gpkg", dir=output.parent); os.close(descriptor); temporary = Path(name); temporary.unlink()
        try:
            for index, (layer, frame) in enumerate(layers.items()): frame.to_file(temporary, layer=layer, driver="GPKG", mode="w" if index == 0 else "a", engine="pyogrio")
            append_original_raster(raster, temporary, width, height)
            append_georeferenced_raster(raster, temporary, coefficients, temporary_dir)
            expected_tables = set(layers) | {"carte_originale_1925", "carte_1925_georeferencee"}
            if application_tables(temporary) != expected_tables: raise RuntimeError(f"TABLES_1925_INATTENDUES:{sorted(application_tables(temporary))}")
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    roles = roads.source_role.value_counts().to_dict(); counts = {"road_fragments": int(roles.get("V1_FRAGMENT", 0)), "road_connectors": int(roles.get("PI_RULE_CONNECTOR", 0)), "roads": len(roads), "ocr_lines": len(lines), "place_labels": len(labels), "circle_candidates": len(circles), "localities_before_sea_filter": len(catalog_all), "historical_localities": len(catalog), "human_validated_localities": int(geo_localities.validation_humaine.sum()), "modern_localities": len(modern_localities), "modern_roads": len(modern_roads)}; timings["total"] = round(time.perf_counter()-total_started, 3)
    result = {"status": "C1925_SOURCE_FIRST_CANDIDATE_BUILT", "epistemic_status": "PENDING_COMPARISON_WITH_CURRENT_BEST", "output": str(output), "sha256": sha256(output), "counts": counts, "timings_seconds": timings}; output.with_suffix(".production.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8"); return result


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--config", type=Path, required=True); mode = parser.add_mutually_exclusive_group(required=True); mode.add_argument("--check", action="store_true"); mode.add_argument("--run", action="store_true"); return parser.parse_args()


def main():
    args = parse_args()
    if args.check:
        config = json.loads(args.config.resolve(strict=True).read_text(encoding="utf-8")); root = args.config.resolve().parent.parent; required = [root/config[key] for key in ["raster", "road_extraction_config", "human_locality_decisions", "m0_control_coordinates", "geonames", "osm_pbf", "tesseract_executable"]]; missing = [str(path) for path in required if not path.is_file()]; result = {"status": "PASS_C1925_SOURCE_FIRST_PREFLIGHT" if not missing else "FAIL_C1925_SOURCE_FIRST_PREFLIGHT", "missing": missing, "pipeline_executed": False}
        if missing: raise FileNotFoundError(result)
    else:
        result = build_product_1925(args.config)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
