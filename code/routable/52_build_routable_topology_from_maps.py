#!/usr/bin/env python3
"""Repair map-derived road topology with endpoint-to-edge continuations only."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import xml.sax.saxutils as xml_escape
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
import pyogrio
from path_utils import resolve_project_path
from PIL import Image
from pyproj import Geod
from shapely.geometry import LineString, Point
from shapely.ops import substring
from shapely.strtree import STRtree


GEOD = Geod(ellps="WGS84")
EPS = 1e-8
# The annual rasters are trusted local research inputs and can legitimately
# exceed Pillow's generic decompression-bomb threshold.
Image.MAX_IMAGE_PIXELS = None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def paired_geographic_subline(map_line: LineString, geo_line: LineString, start: float, end: float) -> LineString:
    map_coords = list(map_line.coords)
    geo_coords = list(geo_line.coords)
    if len(map_coords) != len(geo_coords):
        raise RuntimeError("VERTEX_LINEAGE_MISMATCH")
    segment_lengths = np.array([math.dist(map_coords[index], map_coords[index + 1]) for index in range(len(map_coords) - 1)], dtype=float)
    cumulative = np.r_[0.0, np.cumsum(segment_lengths)]

    def location(distance: float) -> tuple[int, tuple[float, float]]:
        if distance >= cumulative[-1] - EPS:
            return len(segment_lengths) - 1, tuple(geo_coords[-1][:2])
        index = int(np.searchsorted(cumulative, distance, side="right") - 1)
        fraction = 0.0 if segment_lengths[index] <= EPS else (distance - cumulative[index]) / segment_lengths[index]
        gx = geo_coords[index][0] + fraction * (geo_coords[index + 1][0] - geo_coords[index][0])
        gy = geo_coords[index][1] + fraction * (geo_coords[index + 1][1] - geo_coords[index][1])
        return index, (float(gx), float(gy))

    start_index, start_coordinate = location(start)
    end_index, end_coordinate = location(end)
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


def paired_geographic_point(map_line: LineString, geo_line: LineString, distance: float) -> Point:
    subline = paired_geographic_subline(map_line, geo_line, 0.0, distance)
    return Point(subline.coords[-1])


def outward_direction(geometry: LineString, at_start: bool) -> np.ndarray:
    coordinates = list(geometry.coords)
    anchor = np.array(coordinates[0 if at_start else -1][:2], dtype=float)
    iterable = coordinates[1:] if at_start else list(reversed(coordinates[:-1]))
    for coordinate in iterable:
        inside = np.array(coordinate[:2], dtype=float)
        vector = anchor - inside
        norm = float(np.linalg.norm(vector))
        if norm > EPS:
            return vector / norm
    raise RuntimeError("ZERO_LENGTH_TERMINAL_EDGE")


def component_count(edges: gpd.GeoDataFrame) -> tuple[int, int]:
    graph = nx.Graph()
    graph.add_edges_from(zip(edges.from_node.astype(str), edges.to_node.astype(str)))
    components = list(nx.connected_components(graph))
    return len(components), max((len(component) for component in components), default=0)


def write_layer(frame: gpd.GeoDataFrame, path: Path, layer: str, first: bool = False) -> None:
    if first and path.exists():
        path.unlink()
    pyogrio.write_dataframe(frame, path, layer=layer, driver="GPKG", append=path.exists())


def qgis_project(path: Path, geopackage: Path, layers: list[tuple[str, str, bool, str]]) -> None:
    project_layers = []
    tree_layers = []
    for index, (layer, label, visible, geometry) in enumerate(layers, 1):
        layer_id = f"{layer}_{index}"
        datasource = f"{geopackage.as_posix()}|layername={layer}"
        project_layers.append(f'<maplayer type="vector" geometry="{geometry}" readOnly="1"><id>{layer_id}</id><layername>{xml_escape.escape(label)}</layername><datasource>{xml_escape.escape(datasource)}</datasource><provider encoding="UTF-8">ogr</provider></maplayer>')
        checked = "Qt::Checked" if visible else "Qt::Unchecked"
        tree_layers.append(f'<layer-tree-layer checked="{checked}" id="{layer_id}" name="{xml_escape.escape(label)}" providerKey="ogr" source="{xml_escape.escape(datasource)}"/>')
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<qgis projectname="Topologie parcourable réparée" version="3.34.0"><layer-tree-group checked="Qt::Checked" name="Topologie parcourable">' + "".join(tree_layers) + '</layer-tree-group><projectlayers>' + "".join(project_layers) + '</projectlayers></qgis>\n'
    path.write_text(xml, encoding="utf-8")


def build_year(year: int, source_path: Path, raster_path: Path, rule_path: Path, output: Path, tolerance: float, max_gap: float, alignment_min: float) -> dict[str, object]:
    annual_rules = json.loads(rule_path.read_text(encoding="utf-8"))
    gap_rule = annual_rules["gap_rule"]
    prescribed_max_gap = float(gap_rule["maximum_distance_px"])
    prescribed_alignment = float(gap_rule["minimum_alignment_dot"])
    if abs(max_gap - prescribed_max_gap) > EPS or abs(alignment_min - prescribed_alignment) > EPS:
        raise RuntimeError(
            f"C{year}:CONFIG_DOES_NOT_MATCH_CONTROLLING_ANNUAL_RULES:"
            f"max_gap={max_gap}/{prescribed_max_gap};alignment={alignment_min}/{prescribed_alignment}"
        )
    map_edges = pyogrio.read_dataframe(source_path, layer="routes_topologiques_larges")
    geo_edges = pyogrio.read_dataframe(source_path, layer="routes_topologiques_larges_geographiques")
    map_nodes = pyogrio.read_dataframe(source_path, layer="noeuds_topologiques")
    geo_nodes = pyogrio.read_dataframe(source_path, layer="noeuds_topologiques_geographiques")
    selector = lambda frame: frame[(frame.tolerance_px == tolerance) & frame.graph_variant.eq("LARGE")].copy().reset_index(drop=True)
    map_edges, geo_edges, map_nodes, geo_nodes = map(selector, (map_edges, geo_edges, map_nodes, geo_nodes))
    if set(map_edges.topo_edge_id.astype(str)) != set(geo_edges.topo_edge_id.astype(str)):
        raise RuntimeError(f"C{year}:MAP_GEO_EDGE_IDS_MISMATCH")
    if set(map_nodes.node_id.astype(str)) != set(geo_nodes.node_id.astype(str)):
        raise RuntimeError(f"C{year}:MAP_GEO_NODE_IDS_MISMATCH")
    geo_edges = geo_edges.set_index("topo_edge_id").loc[map_edges.topo_edge_id].reset_index()
    geo_node_lookup = geo_nodes.set_index("node_id").geometry.to_dict()

    incidence: dict[str, list[int]] = defaultdict(list)
    base_graph = nx.Graph()
    for index, row in map_edges.iterrows():
        left, right = str(row.from_node), str(row.to_node)
        incidence[left].append(index)
        incidence[right].append(index)
        base_graph.add_edge(left, right)
    initial_components = list(nx.connected_components(base_graph))
    component_lookup = {node: number for number, component in enumerate(initial_components) for node in component}
    geometries = list(map_edges.geometry)
    tree = STRtree(geometries)

    proposals: list[dict[str, object]] = []
    for node in map_nodes.loc[map_nodes.degree.eq(1), "node_id"].astype(str):
        incident_index = incidence[node][0]
        incident = map_edges.iloc[incident_index]
        at_start = str(incident.from_node) == node
        point = Point(incident.geometry.coords[0 if at_start else -1])
        direction = outward_direction(incident.geometry, at_start)
        candidates = []
        for target_index in tree.query(point, predicate="dwithin", distance=max_gap + EPS).tolist():
            if target_index == incident_index:
                continue
            target = geometries[target_index]
            position = float(target.project(point))
            projected = target.interpolate(position)
            vector = np.array([projected.x - point.x, projected.y - point.y], dtype=float)
            gap = float(np.linalg.norm(vector))
            if gap <= tolerance + EPS or gap > max_gap + EPS:
                continue
            alignment = float(np.dot(direction, vector / gap))
            if alignment + EPS < alignment_min:
                continue
            candidates.append((gap, -alignment, str(map_edges.iloc[target_index].topo_edge_id), target_index, position, projected, alignment))
        if not candidates:
            continue
        gap, _, target_id, target_index, position, projected, alignment = min(candidates)
        target = map_edges.iloc[target_index]
        proposals.append({
            "origin_node": node,
            "origin_edge_id": str(incident.topo_edge_id),
            "target_edge_id": target_id,
            "target_edge_index": int(target_index),
            "target_position": float(position),
            "gap_px": float(gap),
            "alignment_dot": float(alignment),
            "origin_base_component": int(component_lookup[node]),
            "target_base_component": int(component_lookup[str(target.from_node)]),
            "geometry": LineString([point, projected]),
        })
    proposal_frame = gpd.GeoDataFrame(proposals, geometry="geometry", crs=None).sort_values(["gap_px", "origin_node"]).reset_index(drop=True)
    proposal_frame["repair_id"] = [f"C{year}_T_REPAIR_{index:05d}" for index in range(1, len(proposal_frame) + 1)]

    split_events: dict[int, list[tuple[float, str]]] = defaultdict(list)
    target_node_coordinates: dict[str, Point] = {}
    target_geo_coordinates: dict[str, Point] = {}
    target_key_to_node: dict[tuple[int, float], str] = {}
    connector_rows = []
    connector_geo_rows = []
    for row in proposal_frame.itertuples():
        target_index = int(row.target_edge_index)
        target_map = map_edges.iloc[target_index].geometry
        target_geo = geo_edges.iloc[target_index].geometry
        if row.target_position <= EPS:
            target_node = str(map_edges.iloc[target_index].from_node)
            target_map_point = Point(target_map.coords[0])
            target_geo_point = geo_node_lookup[target_node]
        elif target_map.length - row.target_position <= EPS:
            target_node = str(map_edges.iloc[target_index].to_node)
            target_map_point = Point(target_map.coords[-1])
            target_geo_point = geo_node_lookup[target_node]
        else:
            key = (target_index, round(float(row.target_position), 8))
            if key not in target_key_to_node:
                target_key_to_node[key] = f"C{year}_REPAIR_NODE_{len(target_key_to_node) + 1:06d}"
            target_node = target_key_to_node[key]
            target_map_point = target_map.interpolate(float(row.target_position))
            target_geo_point = paired_geographic_point(target_map, target_geo, float(row.target_position))
            target_node_coordinates[target_node] = target_map_point
            target_geo_coordinates[target_node] = target_geo_point
            split_events[target_index].append((float(row.target_position), target_node))
        origin_geo_point = geo_node_lookup[str(row.origin_node)]
        connector_geo = LineString([origin_geo_point, target_geo_point])
        common = {
            "topo_edge_id": str(row.repair_id),
            "map_year": year,
            "from_node": str(row.origin_node),
            "to_node": target_node,
            "source_edge_id": str(row.repair_id),
            "source_feature_id": str(row.repair_id),
            "source_role": "TOPOLOGY_T_JUNCTION_REPAIR",
            "repair_kind": "ENDPOINT_TO_EDGE_CONTINUATION",
            "origin_edge_id": str(row.origin_edge_id),
            "target_edge_id": str(row.target_edge_id),
            "gap_px": float(row.gap_px),
            "alignment_dot": float(row.alignment_dot),
            "new_connector_created": 1,
        }
        connector_rows.append({**common, "pixel_length": float(row.geometry.length), "geometry": row.geometry})
        connector_geo_rows.append({**common, "geodesic_km": abs(float(GEOD.geometry_length(connector_geo))) / 1000.0, "geometry": connector_geo})

    repaired_map_rows = []
    repaired_geo_rows = []
    lineage_rows = []
    for edge_index, edge in map_edges.iterrows():
        geo_edge = geo_edges.iloc[edge_index]
        events = [(0.0, str(edge.from_node)), (float(edge.geometry.length), str(edge.to_node))] + split_events.get(edge_index, [])
        events = sorted(events, key=lambda item: (item[0], item[1]))
        deduplicated: list[tuple[float, str]] = []
        for distance, node in events:
            if deduplicated and abs(distance - deduplicated[-1][0]) <= EPS:
                continue
            deduplicated.append((distance, node))
        for part, ((start, left), (end, right)) in enumerate(zip(deduplicated, deduplicated[1:]), 1):
            if end - start <= EPS:
                continue
            map_geometry = substring(edge.geometry, start, end)
            geo_geometry = paired_geographic_subline(edge.geometry, geo_edge.geometry, start, end)
            edge_id = f"{edge.topo_edge_id}_R{part:03d}"
            base = {column: edge[column] for column in map_edges.columns if column != "geometry"}
            base.update({"topo_edge_id": edge_id, "from_node": left, "to_node": right, "pixel_length": float(map_geometry.length), "repair_kind": "SOURCE_TOPOLOGY_PRESERVED", "origin_edge_id": str(edge.topo_edge_id), "target_edge_id": "", "gap_px": 0.0, "alignment_dot": 1.0})
            repaired_map_rows.append({**base, "geometry": map_geometry})
            repaired_geo_rows.append({**base, "geodesic_km": abs(float(GEOD.geometry_length(geo_geometry))) / 1000.0, "geometry": geo_geometry})
            lineage_rows.append({"repaired_edge_id": edge_id, "object_type": "SOURCE_SPLIT", "source_topo_edge_id": str(edge.topo_edge_id), "source_edge_id": str(edge.source_edge_id), "source_fraction_start": float(start / edge.geometry.length), "source_fraction_end": float(end / edge.geometry.length), "repair_id": ""})
    for map_row, geo_row in zip(connector_rows, connector_geo_rows):
        repaired_map_rows.append(map_row)
        repaired_geo_rows.append(geo_row)
        lineage_rows.append({"repaired_edge_id": map_row["topo_edge_id"], "object_type": "TOPOLOGY_REPAIR", "source_topo_edge_id": map_row["origin_edge_id"], "source_edge_id": map_row["source_edge_id"], "source_fraction_start": np.nan, "source_fraction_end": np.nan, "repair_id": map_row["topo_edge_id"]})

    repaired_map = gpd.GeoDataFrame(repaired_map_rows, geometry="geometry", crs=None)
    repaired_geo = gpd.GeoDataFrame(repaired_geo_rows, geometry="geometry", crs="EPSG:4326")
    repaired_graph = nx.Graph()
    for row in repaired_map.itertuples():
        repaired_graph.add_edge(str(row.from_node), str(row.to_node), weight=float(row.pixel_length))
    components = list(nx.connected_components(repaired_graph))
    repaired_component_lookup = {node: f"C{year}_REPAIRED_COMPONENT_{index:04d}" for index, component in enumerate(sorted(components, key=len, reverse=True), 1) for node in component}
    repaired_map["component_id"] = repaired_map.from_node.astype(str).map(repaired_component_lookup)
    repaired_geo["component_id"] = repaired_geo.from_node.astype(str).map(repaired_component_lookup)

    node_map_lookup = map_nodes.set_index("node_id").geometry.to_dict()
    node_map_lookup.update(target_node_coordinates)
    node_geo_lookup = dict(geo_node_lookup)
    node_geo_lookup.update(target_geo_coordinates)
    degree = dict(repaired_graph.degree())
    node_rows = [{"node_id": node, "map_year": year, "degree": int(degree[node]), "component_id": repaired_component_lookup[node], "node_role": "REPAIR_SPLIT" if node in target_node_coordinates else "SOURCE_TOPOLOGY", "geometry": node_map_lookup[node]} for node in repaired_graph.nodes]
    geo_node_rows = [{**{key: value for key, value in row.items() if key != "geometry"}, "geometry": node_geo_lookup[row["node_id"]]} for row in node_rows]
    repaired_nodes = gpd.GeoDataFrame(node_rows, geometry="geometry", crs=None)
    repaired_geo_nodes = gpd.GeoDataFrame(geo_node_rows, geometry="geometry", crs="EPSG:4326")
    connector_map = gpd.GeoDataFrame(connector_rows, geometry="geometry", crs=None)
    connector_geo = gpd.GeoDataFrame(connector_geo_rows, geometry="geometry", crs="EPSG:4326")

    source_length = float(map_edges.pixel_length.sum())
    preserved_length = float(repaired_map.loc[repaired_map.source_role.ne("TOPOLOGY_T_JUNCTION_REPAIR"), "pixel_length"].sum())
    initial_count, initial_largest = component_count(map_edges)
    repaired_count, repaired_largest = component_count(repaired_map)
    validation = pd.DataFrame([
        {"check": "NO_PAIR_TABLE_INPUT", "status": "PASS", "detail": "construction uses only map-derived topology and raster for control"},
        {"check": "SOURCE_EDGE_COVERAGE", "status": "PASS" if set(map_edges.topo_edge_id.astype(str)) == set(pd.DataFrame(lineage_rows).loc[lambda d: d.object_type.eq("SOURCE_SPLIT"), "source_topo_edge_id"].astype(str)) else "FAIL", "detail": f"{map_edges.topo_edge_id.nunique()} source topology edges"},
        {"check": "SOURCE_LENGTH_PRESERVED", "status": "PASS" if abs(source_length - preserved_length) <= max(1e-6, source_length * 1e-10) else "FAIL", "detail": f"source={source_length:.9f}; repaired_source={preserved_length:.9f}"},
        {"check": "GAP_THRESHOLD", "status": "PASS" if connector_map.empty or connector_map.gap_px.le(max_gap + EPS).all() else "FAIL", "detail": f"max={connector_map.gap_px.max() if not connector_map.empty else 0:.6f}"},
        {"check": "ALIGNMENT_THRESHOLD", "status": "PASS" if connector_map.empty or connector_map.alignment_dot.ge(alignment_min - EPS).all() else "FAIL", "detail": f"min={connector_map.alignment_dot.min() if not connector_map.empty else 1:.6f}"},
        {"check": "POSITIVE_EDGE_LENGTHS", "status": "PASS" if repaired_map.pixel_length.gt(0).all() and repaired_geo.geodesic_km.gt(0).all() else "FAIL", "detail": f"edges={len(repaired_map)}"},
        {"check": "CONNECTIVITY_IMPROVES", "status": "PASS" if repaired_count < initial_count and repaired_largest > initial_largest else "FAIL", "detail": f"components {initial_count}->{repaired_count}; largest nodes {initial_largest}->{repaired_largest}"},
        {"check": "MAP_GEO_EDGE_IDS", "status": "PASS" if set(repaired_map.topo_edge_id.astype(str)) == set(repaired_geo.topo_edge_id.astype(str)) else "FAIL", "detail": f"{len(repaired_map)} edges"},
    ])
    if validation.status.ne("PASS").any():
        raise RuntimeError(f"C{year}:REPAIR_VALIDATION_FAILED:" + ",".join(validation.loc[validation.status.ne("PASS"), "check"]))

    output.mkdir(parents=True, exist_ok=True)
    gpkg = output / f"C{year}_ROUTABLE_TOPOLOGY_REPAIRED.gpkg"
    write_layer(repaired_map, gpkg, "repaired_edges_map", first=True)
    write_layer(repaired_geo, gpkg, "repaired_edges_geographic")
    write_layer(connector_map, gpkg, "repair_connectors_map")
    write_layer(connector_geo, gpkg, "repair_connectors_geographic")
    write_layer(repaired_nodes, gpkg, "repaired_nodes_map")
    write_layer(repaired_geo_nodes, gpkg, "repaired_nodes_geographic")
    lineage = pd.DataFrame(lineage_rows)
    repair_register = proposal_frame.drop(columns="geometry")
    validation.to_csv(output / f"C{year}_ROUTABLE_TOPOLOGY_VALIDATION.csv", index=False)
    lineage.to_csv(output / f"C{year}_ROUTABLE_TOPOLOGY_LINEAGE.csv", index=False)
    repair_register.to_csv(output / f"C{year}_ROUTABLE_TOPOLOGY_REPAIRS.csv", index=False)
    with sqlite3.connect(gpkg) as connection:
        lineage.to_sql("edge_lineage", connection, if_exists="replace", index=False)
        validation.to_sql("technical_validation", connection, if_exists="replace", index=False)
        repair_register.to_sql("repair_proposals", connection, if_exists="replace", index=False)

    qgis_project(output / f"C{year}_ROUTABLE_TOPOLOGY_CONTROL.qgs", gpkg, [
        ("repaired_edges_map", "Routes topologiques réparées", True, "Line"),
        ("repair_connectors_map", "Raccordements topologiques ajoutés", True, "Line"),
        ("repaired_nodes_map", "Nœuds réparés", False, "Point"),
    ])

    fig, axis = plt.subplots(figsize=(18, 15), constrained_layout=True)
    repaired_map.loc[repaired_map.source_role.ne("TOPOLOGY_T_JUNCTION_REPAIR")].plot(ax=axis, color="#4d4d4d", linewidth=0.35, alpha=0.65)
    if not connector_map.empty:
        connector_map.plot(ax=axis, color="#d7191c", linewidth=1.0, alpha=0.95)
    axis.set_title(f"{year} — réseau dérivé de la carte, raccordements topologiques en rouge")
    axis.set_axis_off()
    fig.savefig(output / f"C{year}_ROUTABLE_TOPOLOGY_OVERVIEW_HIGHRES.png", dpi=350, facecolor="white", bbox_inches="tight")
    plt.close(fig)

    selected = connector_map.sort_values("gap_px", ascending=False).head(16)
    fig, axes = plt.subplots(4, 4, figsize=(20, 20), constrained_layout=True)
    with Image.open(raster_path) as raster_image:
        width, height = raster_image.size
        for axis, row in zip(axes.ravel(), selected.itertuples()):
            x, y = row.geometry.centroid.x, row.geometry.centroid.y
            radius = 90.0
            left = max(0, int(math.floor(x - radius)))
            right = min(width, int(math.ceil(x + radius)))
            top = max(0, int(math.floor(height - (y + radius))))
            bottom = min(height, int(math.ceil(height - (y - radius))))
            crop = np.asarray(raster_image.crop((left, top, right, bottom)).convert("RGB"))
            axis.imshow(crop, extent=(left, right, height - bottom, height - top), origin="upper")
            repaired_map.cx[x-radius:x+radius, y-radius:y+radius].plot(ax=axis, color="#2166ac", linewidth=1.0, alpha=0.8)
            connector_map.cx[x-radius:x+radius, y-radius:y+radius].plot(ax=axis, color="#d7191c", linewidth=2.2)
            axis.set_xlim(x-radius, x+radius); axis.set_ylim(y-radius, y+radius); axis.set_axis_off()
            axis.set_title(f"{row.topo_edge_id} — {row.gap_px:.1f}px; align. {row.alignment_dot:.2f}", fontsize=9)
    for axis in axes.ravel()[len(selected):]:
        axis.set_axis_off()
    fig.suptitle(f"{year} — agrandissements des raccordements les plus longs", fontsize=16)
    fig.savefig(output / f"C{year}_ROUTABLE_TOPOLOGY_REPAIR_ZOOMS_HIGHRES.png", dpi=300, facecolor="white", bbox_inches="tight")
    plt.close(fig)

    result = {
        "map_year": year,
        "generated_at": datetime.now(ZoneInfo("Africa/Casablanca")).isoformat(timespec="seconds"),
        "source_topology": str(source_path),
        "source_topology_sha256": sha256(source_path),
        "source_raster": str(raster_path),
        "source_raster_sha256": sha256(raster_path),
        "controlling_annual_road_rules": str(rule_path),
        "controlling_annual_road_rules_sha256": sha256(rule_path),
        "method": "endpoint-to-edge continuation using the controlling map gap thresholds; no city-pair input",
        "tolerance_px": tolerance,
        "maximum_gap_px": max_gap,
        "minimum_alignment_dot": alignment_min,
        "initial_edges": len(map_edges),
        "repair_connectors": len(connector_map),
        "repaired_edges": len(repaired_map),
        "initial_components": initial_count,
        "repaired_components": repaired_count,
        "initial_largest_component_nodes": initial_largest,
        "repaired_largest_component_nodes": repaired_largest,
        "output_geopackage": str(gpkg),
        "output_sha256": sha256(gpkg),
        "status": "TECHNICALLY_VERIFIED_MAP_DERIVED_ROUTABLE_TOPOLOGY_PENDING_HUMAN_VISUAL_CONTROL",
    }
    (output / f"C{year}_ROUTABLE_TOPOLOGY_RECEIPT.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--year", type=int, help="Build one configured year; useful for parallel execution")
    args = parser.parse_args()
    config = json.loads(resolve_project_path(args.config).read_text(encoding="utf-8"))
    output_root = resolve_project_path(config["output_dir"])
    years = [int(year) for year in config["years"]]
    if args.year is not None:
        if args.year not in years:
            raise RuntimeError(f"YEAR_NOT_CONFIGURED:{args.year}")
        years = [args.year]
    results = []
    for year in years:
        output = output_root / str(year) if config.get("separate_year_directories", False) else output_root
        annual = config.get("parameters_by_year", {}).get(str(year), {})
        tolerance = float(annual.get("tolerance_px", config.get("tolerance_px", 1.0)))
        max_gap = float(annual.get("maximum_gap_px", config.get("maximum_gap_px")))
        alignment = float(annual.get("minimum_alignment_dot", config.get("minimum_alignment_dot")))
        results.append(build_year(
            year,
            resolve_project_path(config["source_topology"][str(year)]),
            resolve_project_path(config["source_raster"][str(year)]),
            resolve_project_path(config["source_road_rules"][str(year)]),
            output,
            tolerance,
            max_gap,
            alignment,
        ))
    output_root.mkdir(parents=True, exist_ok=True)
    summary_name = f"ROUTABLE_TOPOLOGY_REPAIR_SUMMARY_{args.year}.csv" if args.year is not None else "ROUTABLE_TOPOLOGY_REPAIR_SUMMARY.csv"
    pd.DataFrame(results).to_csv(output_root / summary_name, index=False)
    print(json.dumps({"status": "PASS", "years": [item["map_year"] for item in results], "output": str(output_root)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
