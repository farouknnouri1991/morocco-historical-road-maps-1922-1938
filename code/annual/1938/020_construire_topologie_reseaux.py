#!/usr/bin/env python3
"""Construire une topologie dérivée des six réseaux vectoriels existants, sans raster ni nouveau raccordement."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from scipy.spatial import cKDTree
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import substring
from shapely.strtree import STRtree


ROOT = Path(__file__).resolve().parents[2]
PRODUCTS = {
    1922: "C1922_PRODUIT_FINAL_CONSOLIDE.gpkg",
    1925: "C1925_PRODUIT_CONSOLIDE_ESSAI.gpkg",
    1928: "C1928_PRODUIT_FINAL_CONSOLIDE.gpkg",
    1934: "C1934_PRODUIT_FINAL_CONSOLIDE.gpkg",
    1936: "C1936_PRODUIT_FINAL_CONSOLIDE.gpkg",
    1938: "C1938_PRODUIT_FINAL_CONSOLIDE.gpkg",
}
TOLERANCES = (0.0, 0.5, 1.0)
VARIANTS = ("STRICT", "LARGE")
EPS = 1e-8


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_map_validation(year: int) -> dict[str, object]:
    """Lire seulement le statut scientifique explicite de la carte source."""
    config_path = ROOT / f"REPRODUCTION_{year}" / "inputs" / f"configuration_{year}.json"
    if not config_path.is_file():
        return {}
    config = json.loads(config_path.read_text(encoding="utf-8"))
    return dict(config.get("scientific_validation", {}))


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


@dataclass(frozen=True)
class Endpoint:
    token: str
    line_index: int
    side: int
    x: float
    y: float


def tolerance_code(value: float) -> str:
    return {0.0: "EXACT", 0.5: "T05", 1.0: "T10"}[value]


def endpoint_clusters(lines: gpd.GeoDataFrame, tolerance: float) -> tuple[dict[str, str], dict[str, list[Point]], float]:
    endpoints: list[Endpoint] = []
    for index, geometry in enumerate(lines.geometry):
        coords = list(geometry.coords)
        endpoints.append(Endpoint(f"E{index}:0", index, 0, float(coords[0][0]), float(coords[0][1])))
        endpoints.append(Endpoint(f"E{index}:1", index, 1, float(coords[-1][0]), float(coords[-1][1])))
    points = np.array([(item.x, item.y) for item in endpoints], dtype=float)
    preliminary = UnionFind()
    for item in endpoints:
        preliminary.add(item.token)
    if tolerance <= 0:
        identical: dict[tuple[float, float], str] = {}
        for item in endpoints:
            coordinate = (item.x, item.y)
            if coordinate in identical:
                preliminary.union(item.token, identical[coordinate])
            else:
                identical[coordinate] = item.token
    else:
        for left, right in cKDTree(points).query_pairs(r=tolerance + EPS):
            preliminary.union(endpoints[left].token, endpoints[right].token)
    connected: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(endpoints):
        connected[preliminary.find(item.token)].append(index)
    clusters: list[list[int]] = []
    for component in connected.values():
        component_clusters: list[list[int]] = []
        for endpoint_index in sorted(component, key=lambda idx: (points[idx, 0], points[idx, 1], endpoints[idx].token)):
            chosen = None
            for cluster_index in range(len(component_clusters) - 1, -1, -1):
                cluster = component_clusters[cluster_index]
                if max(np.linalg.norm(points[endpoint_index] - points[member]) for member in cluster) <= tolerance + EPS:
                    chosen = cluster_index
                    break
            if chosen is None:
                component_clusters.append([endpoint_index])
            else:
                component_clusters[chosen].append(endpoint_index)
        clusters.extend(component_clusters)
    token_to_key: dict[str, str] = {}
    key_points: dict[str, list[Point]] = {}
    max_diameter = 0.0
    for cluster_number, members in enumerate(clusters, 1):
        key = f"END_{cluster_number:07d}"
        coordinates = points[members]
        if len(members) > 1:
            diameter = max(float(np.linalg.norm(coordinates[i] - coordinates[j])) for i in range(len(members)) for j in range(i + 1, len(members)))
            max_diameter = max(max_diameter, diameter)
        key_points[key] = [Point(float(points[index, 0]), float(points[index, 1])) for index in members]
        for index in members:
            token_to_key[endpoints[index].token] = key
    return token_to_key, key_points, max_diameter


def intersection_points(geometry) -> list[Point]:
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Point":
        return [geometry]
    if geometry.geom_type == "MultiPoint":
        return list(geometry.geoms)
    if geometry.geom_type == "GeometryCollection":
        return [part for part in geometry.geoms if part.geom_type == "Point"]
    return []


def exact_intersections(lines: gpd.GeoDataFrame) -> tuple[list[dict], list[dict]]:
    geometries = list(lines.geometry)
    pairs = STRtree(geometries).query(geometries, predicate="intersects")
    point_events: list[dict] = []
    overlaps: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for left, right in zip(pairs[0].tolist(), pairs[1].tolist()):
        if left >= right or (left, right) in seen:
            continue
        seen.add((left, right))
        inter = geometries[left].intersection(geometries[right])
        points = intersection_points(inter)
        if not points and not inter.is_empty:
            overlaps.append({"left": left, "right": right, "geometry": inter, "kind": inter.geom_type})
        for point in points:
            dl = float(geometries[left].project(point)); dr = float(geometries[right].project(point))
            point_events.append({
                "left": left,
                "right": right,
                "point": point,
                "left_distance": dl,
                "right_distance": dr,
                "left_endpoint": dl <= EPS or geometries[left].length - dl <= EPS,
                "right_endpoint": dr <= EPS or geometries[right].length - dr <= EPS,
            })
    return point_events, overlaps


def paired_geographic_subline(map_line: LineString, geo_line: LineString, start: float, end: float) -> LineString:
    map_coords = list(map_line.coords)
    geo_coords = list(geo_line.coords)
    if len(map_coords) != len(geo_coords):
        raise RuntimeError("VERTEX_LINEAGE_MISMATCH")
    segment_lengths = [math.dist(map_coords[index], map_coords[index + 1]) for index in range(len(map_coords) - 1)]
    cumulative = np.r_[0.0, np.cumsum(segment_lengths)]

    def location(distance: float) -> tuple[int, float, tuple[float, float]]:
        if distance >= cumulative[-1] - EPS:
            return len(segment_lengths) - 1, 1.0, tuple(geo_coords[-1][:2])
        index = int(np.searchsorted(cumulative, distance, side="right") - 1)
        length = segment_lengths[index]
        fraction = 0.0 if length <= EPS else (distance - cumulative[index]) / length
        gx = geo_coords[index][0] + fraction * (geo_coords[index + 1][0] - geo_coords[index][0])
        gy = geo_coords[index][1] + fraction * (geo_coords[index + 1][1] - geo_coords[index][1])
        return index, fraction, (float(gx), float(gy))

    start_index, _, start_coordinate = location(start)
    end_index, _, end_coordinate = location(end)
    coordinates = [start_coordinate]
    for vertex in range(start_index + 1, end_index + 1):
        vertex_distance = float(cumulative[vertex])
        if start + EPS < vertex_distance < end - EPS:
            coordinates.append(tuple(geo_coords[vertex][:2]))
    coordinates.append(end_coordinate)
    cleaned = [coordinates[0]]
    for coordinate in coordinates[1:]:
        if coordinate != cleaned[-1]:
            cleaned.append(coordinate)
    if len(cleaned) < 2:
        cleaned.append(end_coordinate)
    return LineString(cleaned)


def build_mode(
    year: int,
    lines: gpd.GeoDataFrame,
    geo_lookup: dict[str, LineString],
    tolerance: float,
    variant: str,
    point_events: list[dict],
    overlaps: list[dict],
) -> dict[str, object]:
    token_to_key, key_points, cluster_diameter = endpoint_clusters(lines, tolerance)
    uf = UnionFind()
    node_types: dict[str, set[str]] = defaultdict(set)
    node_gaps: dict[str, list[float]] = defaultdict(list)
    for key, points in key_points.items():
        uf.add(key); node_types[key].add("ENDPOINT"); node_gaps[key].extend([0.0] * len(points))
    splits: dict[int, list[tuple[float, str, str, float]]] = defaultdict(list)
    for index, geometry in enumerate(lines.geometry):
        start_key = token_to_key[f"E{index}:0"]; end_key = token_to_key[f"E{index}:1"]
        splits[index].append((0.0, start_key, "ENDPOINT", 0.0))
        splits[index].append((float(geometry.length), end_key, "ENDPOINT", 0.0))
    ambiguous_rows: list[dict] = []
    for event_number, event in enumerate(point_events, 1):
        left, right = int(event["left"]), int(event["right"])
        point = event["point"]
        left_end, right_end = bool(event["left_endpoint"]), bool(event["right_endpoint"])
        if left_end and right_end:
            left_side = 0 if event["left_distance"] <= EPS else 1
            right_side = 0 if event["right_distance"] <= EPS else 1
            uf.union(token_to_key[f"E{left}:{left_side}"], token_to_key[f"E{right}:{right_side}"])
        elif left_end or right_end:
            endpoint_line = left if left_end else right
            target_line = right if left_end else left
            endpoint_distance = event["left_distance"] if left_end else event["right_distance"]
            target_distance = event["right_distance"] if left_end else event["left_distance"]
            endpoint_side = 0 if endpoint_distance <= EPS else 1
            key = token_to_key[f"E{endpoint_line}:{endpoint_side}"]
            splits[target_line].append((float(target_distance), key, "T_JUNCTION_EXACT", 0.0))
            key_points[key].append(point); node_types[key].add("T_JUNCTION_EXACT")
        else:
            connector_involved = str(lines.iloc[left].source_role) == "PI_RULE_CONNECTOR" or str(lines.iloc[right].source_role) == "PI_RULE_CONNECTOR"
            key = f"X_{round(point.x, 8)}_{round(point.y, 8)}"
            join = variant == "LARGE" or connector_involved
            if join:
                uf.add(key); key_points.setdefault(key, []).append(point)
                kind = "X_EXISTING_CONNECTOR" if connector_involved else "X_AUTOMATIC_WIDE"
                node_types[key].add(kind)
                splits[left].append((float(event["left_distance"]), key, kind, 0.0))
                splits[right].append((float(event["right_distance"]), key, kind, 0.0))
            ambiguous_rows.append({
                "crossing_id": f"C{year}_X_{event_number:07d}",
                "tolerance_px": tolerance,
                "graph_variant": variant,
                "left_edge_id": str(lines.iloc[left].edge_id),
                "right_edge_id": str(lines.iloc[right].edge_id),
                "left_source_role": str(lines.iloc[left].source_role),
                "right_source_role": str(lines.iloc[right].source_role),
                "connector_involved": int(connector_involved),
                "strict_noded": int(connector_involved),
                "wide_noded": 1,
                "noded_in_this_variant": int(join),
                "automatic_wide_crossing": int(not connector_involved),
                "ambiguity_type": "INTERIOR_X_CROSSING",
                "geometry": point,
            })
    max_numeric_gap = 0.0
    if tolerance > 0:
        geometries = list(lines.geometry)
        tree = STRtree(geometries)
        endpoints: list[tuple[int, int, Point, str]] = []
        for index, geometry in enumerate(geometries):
            endpoints.append((index, 0, Point(geometry.coords[0]), token_to_key[f"E{index}:0"]))
            endpoints.append((index, 1, Point(geometry.coords[-1]), token_to_key[f"E{index}:1"]))
        pairs = tree.query([item[2] for item in endpoints], predicate="dwithin", distance=tolerance + EPS)
        candidates: dict[int, list[tuple[float, int, float, Point]]] = defaultdict(list)
        for endpoint_index, target_index in zip(pairs[0].tolist(), pairs[1].tolist()):
            source_index, _, point, _ = endpoints[endpoint_index]
            if source_index == target_index:
                continue
            target = geometries[target_index]
            projected_distance = float(target.project(point))
            if projected_distance <= EPS or target.length - projected_distance <= EPS:
                continue
            projected = target.interpolate(projected_distance)
            gap = float(point.distance(projected))
            if EPS < gap <= tolerance + EPS:
                candidates[endpoint_index].append((gap, target_index, projected_distance, projected))
        for endpoint_index, values in candidates.items():
            minimum = min(value[0] for value in values)
            source_index, side, point, key = endpoints[endpoint_index]
            for gap, target_index, projected_distance, projected in values:
                if gap > minimum + 1e-9:
                    continue
                splits[target_index].append((projected_distance, key, "T_JUNCTION_NUMERIC", gap))
                key_points[key].append(projected); node_types[key].add("T_JUNCTION_NUMERIC"); node_gaps[key].append(gap)
                max_numeric_gap = max(max_numeric_gap, gap)
    for line_index, values in splits.items():
        values.sort(key=lambda value: (value[0], value[1]))
        for previous, current in zip(values, values[1:]):
            if abs(previous[0] - current[0]) <= EPS:
                uf.union(previous[1], current[1])
    merged_points: dict[str, list[Point]] = defaultdict(list)
    merged_types: dict[str, set[str]] = defaultdict(set)
    merged_gaps: dict[str, list[float]] = defaultdict(list)
    for key, points in key_points.items():
        root = uf.find(key); merged_points[root].extend(points); merged_types[root].update(node_types[key]); merged_gaps[root].extend(node_gaps[key])
    ordered_roots = sorted(merged_points, key=lambda key: (np.mean([p.x for p in merged_points[key]]), np.mean([p.y for p in merged_points[key]]), key))
    node_ids = {root: f"C{year}_{tolerance_code(tolerance)}_{variant}_N{index:07d}" for index, root in enumerate(ordered_roots, 1)}
    edge_rows: list[dict] = []
    geo_edge_rows: list[dict] = []
    lineage_rows: list[dict] = []
    edge_number = 0
    for line_index, source in lines.iterrows():
        values = sorted((distance, uf.find(key), kind, gap) for distance, key, kind, gap in splits[line_index])
        deduplicated: list[tuple[float, str]] = []
        for distance, root, _, _ in values:
            if deduplicated and abs(distance - deduplicated[-1][0]) <= EPS:
                deduplicated[-1] = (deduplicated[-1][0], uf.find(root))
            else:
                deduplicated.append((distance, uf.find(root)))
        source_geometry = source.geometry
        source_geo = geo_lookup[str(source.edge_id)]
        for segment_index, ((start, start_root), (end, end_root)) in enumerate(zip(deduplicated, deduplicated[1:]), 1):
            if end - start <= EPS:
                continue
            geometry = substring(source_geometry, start, end)
            if geometry.geom_type != "LineString" or geometry.length <= EPS:
                continue
            edge_number += 1
            topo_id = f"C{year}_{tolerance_code(tolerance)}_{variant}_E{edge_number:08d}"
            base = {
                "topo_edge_id": topo_id,
                "map_year": year,
                "tolerance_px": tolerance,
                "graph_variant": variant,
                "from_node": node_ids[uf.find(start_root)],
                "to_node": node_ids[uf.find(end_root)],
                "source_edge_id": str(source.edge_id),
                "source_feature_id": str(source.source_feature_id),
                "source_role": str(source.source_role),
                "source_segment_index": segment_index,
                "source_fraction_start": float(start / source_geometry.length),
                "source_fraction_end": float(end / source_geometry.length),
                "pixel_length": float(geometry.length),
                "new_connector_created": 0,
            }
            edge_rows.append({**base, "geometry": geometry})
            geo_edge_rows.append({**base, "geometry": paired_geographic_subline(source_geometry, source_geo, start, end)})
            lineage_rows.append({
                "map_year": year, "tolerance_px": tolerance, "graph_variant": variant,
                "object_type": "EDGE", "object_id": topo_id, "source_edge_id": str(source.edge_id),
                "source_feature_id": str(source.source_feature_id), "source_role": str(source.source_role),
                "topo_edge_id": topo_id, "corridor_member_order": None,
                "source_fraction_start": float(start / source_geometry.length), "source_fraction_end": float(end / source_geometry.length),
            })
    edges = gpd.GeoDataFrame(edge_rows, geometry="geometry", crs=None)
    geo_edges = gpd.GeoDataFrame(geo_edge_rows, geometry="geometry", crs="EPSG:4326")
    adjacency: dict[str, list[int]] = defaultdict(list)
    graph_uf = UnionFind()
    for index, edge in edges.iterrows():
        left, right = str(edge.from_node), str(edge.to_node)
        adjacency[left].append(index); adjacency[right].append(index)
        graph_uf.union(left, right)
    degree = {node: len(indices) for node, indices in adjacency.items()}
    components: dict[str, list[int]] = defaultdict(list)
    for index, edge in edges.iterrows():
        components[graph_uf.find(str(edge.from_node))].append(index)
    ordered_components = sorted(components.values(), key=lambda members: (-len(members), min(str(edges.loc[index].topo_edge_id) for index in members)))
    component_ids: dict[int, str] = {}
    component_rows: list[dict] = []
    geo_component_rows: list[dict] = []
    for component_number, members in enumerate(ordered_components, 1):
        component_id = f"C{year}_{tolerance_code(tolerance)}_{variant}_C{component_number:06d}"
        nodes = sorted({str(edges.loc[index].from_node) for index in members} | {str(edges.loc[index].to_node) for index in members})
        total_length = float(edges.loc[members].pixel_length.sum())
        for index in members:
            component_ids[index] = component_id
        attributes = {
            "component_id": component_id, "map_year": year, "tolerance_px": tolerance, "graph_variant": variant,
            "edge_count": len(members), "node_count": len(nodes), "source_edge_count": int(edges.loc[members].source_edge_id.nunique()),
            "connector_source_count": int(edges.loc[members].loc[edges.loc[members].source_role.eq("PI_RULE_CONNECTOR"), "source_edge_id"].nunique()),
            "pixel_length": total_length,
        }
        component_rows.append({**attributes, "geometry": MultiLineString([edges.loc[index].geometry for index in members])})
        geo_component_rows.append({**attributes, "geometry": MultiLineString([geo_edges.loc[index].geometry for index in members])})
    edges["component_id"] = [component_ids[index] for index in edges.index]
    geo_edges["component_id"] = edges.component_id.values
    edges["from_degree"] = edges.from_node.map(degree).astype(int); edges["to_degree"] = edges.to_node.map(degree).astype(int)
    geo_edges["from_degree"] = edges.from_degree.values; geo_edges["to_degree"] = edges.to_degree.values
    node_rows: list[dict] = []
    for root in ordered_roots:
        node_id = node_ids[root]
        points = merged_points[root]
        x = float(np.mean([point.x for point in points])); y = float(np.mean([point.y for point in points]))
        node_rows.append({
            "node_id": node_id, "map_year": year, "tolerance_px": tolerance, "graph_variant": variant,
            "degree": int(degree.get(node_id, 0)), "node_type": "+".join(sorted(merged_types[root])),
            "automatic_crossing": int("X_AUTOMATIC_WIDE" in merged_types[root]),
            "max_numeric_gap_px": float(max(merged_gaps[root], default=0.0)), "geometry": Point(x, y),
        })
    nodes = gpd.GeoDataFrame(node_rows, geometry="geometry", crs=None)
    geo_node_coordinates: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for _, edge in geo_edges.iterrows():
        geo_node_coordinates[str(edge.from_node)].append(tuple(edge.geometry.coords[0]))
        geo_node_coordinates[str(edge.to_node)].append(tuple(edge.geometry.coords[-1]))
    geo_nodes = nodes.drop(columns="geometry").copy()
    geo_nodes = gpd.GeoDataFrame(geo_nodes, geometry=[Point(float(np.mean([c[0] for c in geo_node_coordinates[node_id]])), float(np.mean([c[1] for c in geo_node_coordinates[node_id]]))) if geo_node_coordinates[node_id] else Point() for node_id in nodes.node_id], crs="EPSG:4326")
    terminations = nodes[nodes.degree <= 1].copy()
    terminations["endpoint_status"] = np.where(terminations.degree.eq(0), "ISOLATED_GRAPH_NODE", "DEGREE_ONE_GRAPH_TERMINATION")
    terminations["historical_interpretation"] = "UNDETERMINED_DO_NOT_INFER_DISCONNECTION"
    used: set[int] = set(); corridor_rows: list[dict] = []; geo_corridor_rows: list[dict] = []

    def walk(start_edge: int, start_node: str) -> list[int]:
        chain: list[int] = []; current_edge = start_edge; current_node = start_node
        while current_edge not in used:
            used.add(current_edge); chain.append(current_edge)
            edge = edges.loc[current_edge]
            next_node = str(edge.to_node) if str(edge.from_node) == current_node else str(edge.from_node)
            if degree.get(next_node, 0) != 2:
                break
            choices = [index for index in adjacency[next_node] if index not in used]
            if not choices:
                break
            current_node, current_edge = next_node, choices[0]
        return chain

    chains: list[list[int]] = []
    for node, incident in sorted(adjacency.items()):
        if degree[node] == 2:
            continue
        for edge_index in incident:
            if edge_index not in used:
                chains.append(walk(edge_index, node))
    for edge_index in edges.index:
        if edge_index not in used:
            chains.append(walk(edge_index, str(edges.loc[edge_index].from_node)))
    source_lookup = lines.set_index("edge_id")
    for corridor_number, members in enumerate(chains, 1):
        corridor_id = f"C{year}_{tolerance_code(tolerance)}_{variant}_R{corridor_number:07d}"
        source_ids = sorted(set(edges.loc[members].source_edge_id.astype(str)))
        attributes = {
            "corridor_id": corridor_id, "map_year": year, "tolerance_px": tolerance, "graph_variant": variant,
            "topo_edge_count": len(members), "source_edge_count": len(source_ids),
            "pixel_length": float(edges.loc[members].pixel_length.sum()),
            "contains_connector": int(edges.loc[members].source_role.eq("PI_RULE_CONNECTOR").any()),
            "topo_edge_ids": ";".join(edges.loc[members].topo_edge_id.astype(str)),
            "source_edge_ids": ";".join(source_ids),
        }
        corridor_rows.append({**attributes, "geometry": MultiLineString([edges.loc[index].geometry for index in members])})
        geo_corridor_rows.append({**attributes, "geometry": MultiLineString([geo_edges.loc[index].geometry for index in members])})
        for member_order, edge_index in enumerate(members, 1):
            member = edges.loc[edge_index]
            source_edge_id = str(member.source_edge_id)
            source = source_lookup.loc[source_edge_id]
            lineage_rows.append({
                "map_year": year, "tolerance_px": tolerance, "graph_variant": variant,
                "object_type": "CORRIDOR", "object_id": corridor_id, "source_edge_id": source_edge_id,
                "source_feature_id": str(source.source_feature_id), "source_role": str(source.source_role),
                "topo_edge_id": str(member.topo_edge_id), "corridor_member_order": member_order,
                "source_fraction_start": float(member.source_fraction_start),
                "source_fraction_end": float(member.source_fraction_end),
            })
    overlap_rows = [{
        "crossing_id": f"C{year}_OVERLAP_{index:07d}", "tolerance_px": tolerance, "graph_variant": variant,
        "left_edge_id": str(lines.iloc[item["left"]].edge_id), "right_edge_id": str(lines.iloc[item["right"]].edge_id),
        "left_source_role": str(lines.iloc[item["left"]].source_role), "right_source_role": str(lines.iloc[item["right"]].source_role),
        "connector_involved": int(str(lines.iloc[item["left"]].source_role) == "PI_RULE_CONNECTOR" or str(lines.iloc[item["right"]].source_role) == "PI_RULE_CONNECTOR"),
        "strict_noded": 0, "wide_noded": 0, "noded_in_this_variant": 0, "automatic_wide_crossing": 0,
        "ambiguity_type": f"OVERLAP_{item['kind']}_NOT_AUTOMATICALLY_NODED", "geometry": item["geometry"].centroid,
    } for index, item in enumerate(overlaps, 1)]
    ambiguous_rows.extend(overlap_rows)
    source_length = float(lines.geometry.length.sum()); output_length = float(edges.geometry.length.sum())
    represented = set(edges.source_edge_id.astype(str))
    missing_sources = sorted(set(lines.edge_id.astype(str)) - represented)
    report = {
        "tolerance_px": tolerance,
        "graph_variant": variant,
        "input_edges": len(lines),
        "input_fragments": int(lines.source_role.eq("V1_FRAGMENT").sum()),
        "input_existing_connectors": int(lines.source_role.eq("PI_RULE_CONNECTOR").sum()),
        "topological_edges": len(edges),
        "nodes": len(nodes),
        "components": len(component_rows),
        "largest_component_edges": max((row["edge_count"] for row in component_rows), default=0),
        "largest_component_source_edges": max((row["source_edge_count"] for row in component_rows), default=0),
        "corridors": len(corridor_rows),
        "ambiguous_crossings": len(ambiguous_rows),
        "automatic_x_crossings_noded": int(sum(row["automatic_wide_crossing"] and row["noded_in_this_variant"] for row in ambiguous_rows)),
        "degree_zero_or_one_graph_terminations": len(terminations),
        "source_length_px": source_length,
        "topological_length_px": output_length,
        "length_difference_px": output_length - source_length,
        "max_endpoint_cluster_diameter_px": cluster_diameter,
        "max_direct_numeric_t_gap_px": max_numeric_gap,
        "missing_source_edges": missing_sources,
        "new_connector_geometry_count": 0,
        "technical_pass": not missing_sources and abs(output_length - source_length) <= max(1e-6, source_length * 1e-10) and cluster_diameter <= tolerance + EPS and max_numeric_gap <= tolerance + EPS,
    }
    return {
        "edges": edges, "geo_edges": geo_edges, "nodes": nodes, "geo_nodes": geo_nodes,
        "corridors": gpd.GeoDataFrame(corridor_rows, geometry="geometry", crs=None),
        "geo_corridors": gpd.GeoDataFrame(geo_corridor_rows, geometry="geometry", crs="EPSG:4326"),
        "components": gpd.GeoDataFrame(component_rows, geometry="geometry", crs=None),
        "geo_components": gpd.GeoDataFrame(geo_component_rows, geometry="geometry", crs="EPSG:4326"),
        "ambiguous": gpd.GeoDataFrame(ambiguous_rows, geometry="geometry", crs=None),
        "terminations": terminations,
        "lineage": pd.DataFrame(lineage_rows),
        "report": report,
    }


def append_frame(path: Path, layer: str, frame: gpd.GeoDataFrame | pd.DataFrame, first: bool = False) -> None:
    if frame.empty:
        return
    if isinstance(frame, gpd.GeoDataFrame):
        frame.to_file(path, layer=layer, driver="GPKG", mode="w" if first else "a", engine="pyogrio")
    else:
        pyogrio.write_dataframe(frame, path, layer=layer, driver="GPKG", append=not first)


def create_sql_views(path: Path) -> list[str]:
    definitions = {
        "vue_routes_strictes_exactes": ("routes_topologiques_strictes", "tolerance_px = 0.0"),
        "vue_routes_larges_1px": ("routes_topologiques_larges", "tolerance_px = 1.0"),
        "vue_routes_strictes_exactes_geo": ("routes_topologiques_strictes_geographiques", "tolerance_px = 0.0"),
        "vue_routes_larges_1px_geo": ("routes_topologiques_larges_geographiques", "tolerance_px = 1.0"),
        "vue_noeuds_stricts_exacts": ("noeuds_topologiques", "tolerance_px = 0.0 AND graph_variant = 'STRICT'"),
        "vue_noeuds_stricts_exacts_geo": ("noeuds_topologiques_geographiques", "tolerance_px = 0.0 AND graph_variant = 'STRICT'"),
        "vue_corridors_stricts_exacts": ("corridors_continus", "tolerance_px = 0.0 AND graph_variant = 'STRICT'"),
    }
    connection = sqlite3.connect(path)
    try:
        for view, (source, where) in definitions.items():
            connection.execute(f'DROP VIEW IF EXISTS "{view}"')
            connection.execute(f'CREATE VIEW "{view}" AS SELECT * FROM "{source}" WHERE {where}')
            content = connection.execute(
                "SELECT data_type, srs_id FROM gpkg_contents WHERE table_name = ?", (source,)
            ).fetchone()
            geometry = connection.execute(
                "SELECT column_name, geometry_type_name, srs_id, z, m FROM gpkg_geometry_columns WHERE table_name = ?", (source,)
            ).fetchone()
            connection.execute("DELETE FROM gpkg_contents WHERE table_name = ?", (view,))
            connection.execute("DELETE FROM gpkg_geometry_columns WHERE table_name = ?", (view,))
            connection.execute(
                "INSERT INTO gpkg_contents(table_name, data_type, identifier, description, last_change, srs_id) VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
                (view, content[0], view, f"Vue filtrée de {source}", content[1]),
            )
            connection.execute(
                "INSERT INTO gpkg_geometry_columns(table_name, column_name, geometry_type_name, srs_id, z, m) VALUES (?, ?, ?, ?, ?, ?)",
                (view, *geometry),
            )
        connection.commit()
    finally:
        connection.close()
    return list(definitions)


def write_qgis_project(path: Path, geopackage: Path, layers: list[tuple[str, str, bool]]) -> None:
    project_layers: list[str] = []
    tree_layers: list[str] = []
    for index, (layer, label, visible) in enumerate(layers, 1):
        layer_id = f"{layer}_{index}"
        datasource = f"{geopackage.as_posix()}|layername={layer}"
        geometry = "Point" if "noeud" in layer or "croisement" in layer or "terminaison" in layer else "Line"
        project_layers.append(
            f'<maplayer type="vector" geometry="{geometry}" simplifyDrawingHints="1" readOnly="1">'
            f'<id>{layer_id}</id><layername>{label}</layername><datasource>{datasource}</datasource>'
            '<provider encoding="UTF-8">ogr</provider></maplayer>'
        )
        checked = "Qt::Checked" if visible else "Qt::Unchecked"
        tree_layers.append(
            f'<layer-tree-layer checked="{checked}" id="{layer_id}" name="{label}" providerKey="ogr" source="{datasource}"/>'
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<qgis projectname="Réseau topologique dérivé" version="3.34.0">'
        f'<layer-tree-group checked="Qt::Checked" name="Réseau topologique">{"".join(tree_layers)}</layer-tree-group>'
        f'<projectlayers>{"".join(project_layers)}</projectlayers>'
        '</qgis>\n'
    )
    path.write_text(xml, encoding="utf-8")


def build_year(year: int) -> dict[str, object]:
    started = time.perf_counter()
    directory = ROOT / f"REPRODUCTION_{year}" / "outputs"
    source_path = directory / PRODUCTS[year]
    output = directory / f"C{year}_RESEAU_TOPOLOGIQUE.gpkg"
    report_path = directory / f"C{year}_RESEAU_TOPOLOGIQUE.controle.json"
    lines = gpd.read_file(source_path, layer=f"routes_{year}_carte")
    geo_lines = gpd.read_file(source_path, layer=f"routes_{year}_geographiques")
    if set(lines.edge_id.astype(str)) != set(geo_lines.edge_id.astype(str)):
        raise RuntimeError(f"C{year}:EDGE_ID_MAP_GEO_MISMATCH")
    geo_lookup = dict(zip(geo_lines.edge_id.astype(str), geo_lines.geometry))
    for edge_id, map_geometry in zip(lines.edge_id.astype(str), lines.geometry):
        if len(map_geometry.coords) != len(geo_lookup[edge_id].coords):
            raise RuntimeError(f"C{year}:VERTEX_LINEAGE_MISMATCH:{edge_id}")
    point_events, overlaps = exact_intersections(lines)
    reports: list[dict] = []
    with tempfile.TemporaryDirectory(prefix=f"c{year}_topology_") as temp_dir:
        temporary = Path(temp_dir) / output.name
        append_frame(temporary, "routes_sources_carte", lines, first=True)
        append_frame(temporary, "routes_sources_geographiques", geo_lines)
        for tolerance in TOLERANCES:
            for variant in VARIANTS:
                result = build_mode(year, lines, geo_lookup, tolerance, variant, point_events, overlaps)
                reports.append(result["report"])
                if not result["report"]["technical_pass"]:
                    raise RuntimeError(f"C{year}:TOPOLOGY_QA_FAILED:{tolerance}:{variant}")
                suffix = "strictes" if variant == "STRICT" else "larges"
                append_frame(temporary, f"routes_topologiques_{suffix}", result["edges"])
                append_frame(temporary, f"routes_topologiques_{suffix}_geographiques", result["geo_edges"])
                append_frame(temporary, "noeuds_topologiques", result["nodes"])
                append_frame(temporary, "noeuds_topologiques_geographiques", result["geo_nodes"])
                append_frame(temporary, "corridors_continus", result["corridors"])
                append_frame(temporary, "corridors_continus_geographiques", result["geo_corridors"])
                append_frame(temporary, "composantes_connexes", result["components"])
                append_frame(temporary, "composantes_connexes_geographiques", result["geo_components"])
                append_frame(temporary, "croisements_ambigus", result["ambiguous"])
                append_frame(temporary, "terminaisons_reseau_a_interpreter", result["terminations"])
                append_frame(temporary, "lineage_edges_sources", result["lineage"])
                del result
        sql_views = create_sql_views(temporary)
        if output.exists():
            output.unlink()
        os.replace(temporary, output)
    qgis_map = directory / f"C{year}_RESEAU_TOPOLOGIQUE_CARTE.qgs"
    qgis_geo = directory / f"C{year}_RESEAU_TOPOLOGIQUE_GEOGRAPHIQUE.qgs"
    write_qgis_project(qgis_map, output, [
        ("routes_sources_carte", "Routes sources", False),
        ("vue_routes_strictes_exactes", "Topologie stricte exacte", True),
        ("vue_routes_larges_1px", "Sensibilité large à 1 pixel", False),
        ("vue_noeuds_stricts_exacts", "Nœuds stricts exacts", True),
        ("croisements_ambigus", "Croisements ambigus", False),
        ("terminaisons_reseau_a_interpreter", "Terminaisons du graphe à interpréter", False),
    ])
    write_qgis_project(qgis_geo, output, [
        ("routes_sources_geographiques", "Routes sources géographiques", False),
        ("vue_routes_strictes_exactes_geo", "Topologie stricte exacte géographique", True),
        ("vue_routes_larges_1px_geo", "Sensibilité large à 1 pixel géographique", False),
        ("vue_noeuds_stricts_exacts_geo", "Nœuds stricts exacts géographiques", True),
    ])
    total_elapsed = round(time.perf_counter() - started, 3)
    validation = source_map_validation(year)
    full_map_validated = bool(validation.get("full_map_validated_by_farouk"))
    report = {
        "status": "TECHNICALLY_COMPLETE_TOPOLOGY_DERIVED_FROM_FAROUK_VALIDATED_MAP" if full_map_validated else "TECHNICALLY_COMPLETE_VECTOR_TOPOLOGY_NOT_HUMAN_VALIDATED",
        "source_map_scientific_status": "FULL_MAP_VALIDATED_BY_FAROUK" if full_map_validated else "NO_FULL_MAP_VALIDATION_RECORDED",
        "source_map_scientific_validation": validation,
        "map_year": year,
        "generated_at": datetime.now(ZoneInfo("Africa/Casablanca")).isoformat(timespec="seconds"),
        "source_geopackage": str(source_path),
        "source_sha256": sha256(source_path),
        "output_geopackage": str(output),
        "output_sha256": sha256(output),
        "qgis_projects": [str(qgis_map), str(qgis_geo)],
        "sql_views": sql_views,
        "method": {
            "rasters_used": False,
            "new_connectors_created": False,
            "existing_connectors_preserved": True,
            "endpoint_tolerances_px": list(TOLERANCES),
            "strict_x_rule": "NO_INTERIOR_X_NODE_UNLESS_EXISTING_PI_RULE_CONNECTOR_INVOLVED",
            "wide_x_rule": "INTERIOR_X_NODED_AND_FLAGGED_AUTOMATIC",
            "corridor_rule": "MERGE_ONLY_THROUGH_DEGREE_TWO_NODES",
            "geographic_rule": "SOURCE_EDGE_VERTEX_LINEAGE_AND_PIXEL_FRACTIONS_NO_KILOMETRIC_DISTANCE",
        },
        "input": {
            "edges": len(lines), "fragments": int(lines.source_role.eq("V1_FRAGMENT").sum()),
            "existing_connectors": int(lines.source_role.eq("PI_RULE_CONNECTOR").sum()),
            "length_px": float(lines.geometry.length.sum()), "exact_point_intersections": len(point_events), "overlap_pairs": len(overlaps),
        },
        "modes": reports,
        "elapsed_seconds": total_elapsed,
        "all_technical_checks_pass": all(item["technical_pass"] for item in reports),
        "scientific_boundary": "Topology is the maximal graph permitted by existing vectors. Missing paths are not evidence of historical disconnection.",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, choices=sorted(PRODUCTS))
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    years = sorted(PRODUCTS) if args.all else [args.year]
    if not args.all and args.year is None:
        parser.error("--year ou --all requis")
    reports = [build_year(year) for year in years]
    print(json.dumps({"status": "PASS", "years": [report["map_year"] for report in reports], "outputs": [report["output_geopackage"] for report in reports]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
