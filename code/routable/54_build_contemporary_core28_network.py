#!/usr/bin/env python3
"""Construct Core-28 contemporary road-network measures comparable to the historical graph."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import geopandas as gpd
import networkx as nx
from networkx.algorithms.connectivity.edge_kcomponents import bridge_components
import numpy as np
import pandas as pd
import pyogrio
from path_utils import resolve_project_path
from pyproj import Geod
from shapely.geometry import LineString, Point


GEOD = Geod(ellps="WGS84")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def node_key(coord) -> tuple[float, float]:
    return (round(float(coord[0]), 7), round(float(coord[1]), 7))


def distance_km(left, right) -> float:
    return abs(float(GEOD.inv(left[0], left[1], right[0], right[1])[2])) / 1000.0


def build_graph(roads: gpd.GeoDataFrame) -> nx.Graph:
    graph = nx.Graph()
    for geometry in roads.geometry:
        coordinates = list(geometry.coords)
        for left, right in zip(coordinates[:-1], coordinates[1:]):
            u, v = node_key(left), node_key(right)
            if u == v:
                continue
            weight = distance_km(u, v)
            if graph.has_edge(u, v):
                if weight < graph[u][v]["weight"]:
                    graph[u][v]["weight"] = weight
            else:
                graph.add_edge(u, v, weight=weight)
    return graph


def nearest_city_snaps(core: pd.DataFrame, roads: gpd.GeoDataFrame) -> pd.DataFrame:
    roads_metric = roads.to_crs(3857)
    cities = gpd.GeoDataFrame(
        core.copy(),
        geometry=[Point(x, y) for x, y in zip(core.centroid_lon, core.centroid_lat)],
        crs="EPSG:4326",
    ).to_crs(3857)
    joined = gpd.sjoin_nearest(
        cities,
        roads_metric[["geometry"]],
        how="left",
        distance_col="projected_snap_distance_m",
    ).reset_index().sort_values(
        ["functional_unit_id_main", "projected_snap_distance_m", "index_right"]
    ).drop_duplicates("functional_unit_id_main")
    records = []
    for row in joined.itertuples(index=False):
        edge_index = int(row.index_right)
        line = roads.loc[edge_index].geometry
        city = Point(float(core.loc[core.functional_unit_id_main.eq(row.functional_unit_id_main), "centroid_lon"].iloc[0]),
                     float(core.loc[core.functional_unit_id_main.eq(row.functional_unit_id_main), "centroid_lat"].iloc[0]))
        projected_position = float(line.project(city))
        snap = line.interpolate(projected_position)
        coordinates = list(line.coords)
        best_segment = None
        for segment_index, (left, right) in enumerate(zip(coordinates[:-1], coordinates[1:])):
            segment = LineString([left, right])
            local_position = float(segment.project(snap))
            candidate = segment.interpolate(local_position)
            candidate_distance = distance_km((city.x, city.y), (candidate.x, candidate.y))
            if best_segment is None or candidate_distance < best_segment[0]:
                best_segment = (candidate_distance, segment_index, local_position, candidate)
        records.append({
            "functional_unit_id_main": row.functional_unit_id_main,
            "source_core28_id": row.source_core28_id,
            "canonical_city_name": row.canonical_city_name,
            "road_index": edge_index,
            "segment_index": int(best_segment[1]),
            "segment_position": float(best_segment[2]),
            "snap_distance_km": float(best_segment[0]),
            "city_lon": city.x,
            "city_lat": city.y,
            "snap_lon": float(best_segment[3].x),
            "snap_lat": float(best_segment[3].y),
        })
    return pd.DataFrame(records)


def insert_city_nodes(graph: nx.Graph, roads: gpd.GeoDataFrame, snaps: pd.DataFrame, radius_km: float) -> tuple[nx.Graph, pd.DataFrame]:
    graph = graph.copy()
    eligible = snaps[snaps.snap_distance_km <= radius_km + 1e-12].copy()
    groups = eligible.groupby(["road_index", "segment_index"], sort=False)
    for (road_index, segment_index), frame in groups:
        coordinates = list(roads.loc[int(road_index)].geometry.coords)
        left, right = node_key(coordinates[int(segment_index)]), node_key(coordinates[int(segment_index) + 1])
        chain = [(0.0, left)]
        for row in frame.sort_values("segment_position").itertuples(index=False):
            chain.append((float(row.segment_position), f"SNAP::{row.functional_unit_id_main}", (row.snap_lon, row.snap_lat)))
        segment_length = LineString([coordinates[int(segment_index)], coordinates[int(segment_index) + 1]]).length
        chain.append((float(segment_length), right))
        if graph.has_edge(left, right):
            graph.remove_edge(left, right)
        expanded = []
        for item in chain:
            position, node = item[0], item[1]
            coordinate = item[2] if len(item) == 3 else node
            if expanded and math.isclose(position, expanded[-1][0], abs_tol=1e-12):
                graph.add_edge(expanded[-1][1], node, weight=0.0)
            else:
                expanded.append((position, node, coordinate))
        for (_, u, coord_u), (_, v, coord_v) in zip(expanded[:-1], expanded[1:]):
            weight = distance_km(coord_u, coord_v)
            graph.add_edge(u, v, weight=weight)
    eligible["attached"] = 1
    return graph, eligible


def retain_city_components_and_contract(graph: nx.Graph, protected_nodes: set[str]) -> nx.Graph:
    keep = set()
    for component in nx.connected_components(graph):
        if component.intersection(protected_nodes):
            keep.update(component)
    graph.remove_nodes_from([node for node in graph if node not in keep])
    anchors = {node for node, degree in graph.degree if degree != 2 or node in protected_nodes}
    contracted = nx.Graph()
    visited_edges = set()
    parallel_index = 0
    for anchor in anchors:
        for neighbor in graph.neighbors(anchor):
            edge_key = frozenset((anchor, neighbor))
            if edge_key in visited_edges:
                continue
            visited_edges.add(edge_key)
            previous, current = anchor, neighbor
            total_weight = float(graph[anchor][neighbor]["weight"])
            while current not in anchors:
                onward = [node for node in graph.neighbors(current) if node != previous]
                if not onward:
                    break
                next_node = onward[0]
                next_key = frozenset((current, next_node))
                if next_key in visited_edges:
                    break
                visited_edges.add(next_key)
                total_weight += float(graph[current][next_node]["weight"])
                previous, current = current, next_node
            if anchor == current:
                continue
            if contracted.has_edge(anchor, current):
                parallel_index += 1
                auxiliary = f"PARALLEL::{parallel_index}"
                contracted.add_edge(anchor, auxiliary, weight=total_weight / 2.0)
                contracted.add_edge(auxiliary, current, weight=total_weight / 2.0)
            else:
                contracted.add_edge(anchor, current, weight=total_weight)
    return contracted


def reachable_core_pair_count(graph: nx.Graph, core_nodes: list[str]) -> int:
    core_set = set(core_nodes)
    total = 0
    for component in nx.connected_components(graph):
        count = len(core_set.intersection(component))
        total += count * (count - 1) // 2
    return total


def core_articulation_flags(graph: nx.Graph, core_nodes: list[str]) -> dict[str, int]:
    flags = {}
    candidates = set(nx.articulation_points(graph))
    for node in core_nodes:
        if node not in candidates:
            flags[node] = 0
            continue
        others = [value for value in core_nodes if value != node]
        baseline = reachable_core_pair_count(graph, others)
        reduced = nx.restricted_view(graph, [node], [])
        flags[node] = int(reachable_core_pair_count(reduced, others) < baseline)
    return flags


def two_edge_component_lookup(graph: nx.Graph) -> dict:
    lookup = {}
    component_id = 0
    for connected_nodes in nx.connected_components(graph):
        subgraph = graph.subgraph(connected_nodes)
        for component in bridge_components(subgraph):
            for node in component:
                lookup[node] = component_id
            component_id += 1
    return lookup


def calculate_metrics(graph: nx.Graph, core: pd.DataFrame, snaps: pd.DataFrame, variant: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    eligible_ids = set(snaps.functional_unit_id_main)
    snap_lookup = snaps.set_index("functional_unit_id_main").to_dict("index")
    core_nodes = [f"SNAP::{unit_id}" for unit_id in sorted(eligible_ids)]
    betweenness = nx.betweenness_centrality_subset(
        graph, sources=core_nodes, targets=core_nodes, normalized=False, weight="weight"
    ) if len(core_nodes) > 1 else {}
    articulation = core_articulation_flags(graph, core_nodes) if len(core_nodes) > 1 else {}
    edge_component = two_edge_component_lookup(graph)
    summaries, pairwise = [], []
    destination_denominator = float(len(core) - 1)
    denominator = ((len(core_nodes) - 1) * (len(core_nodes) - 2) / 2.0) if len(core_nodes) > 2 else np.nan
    for origin in core.itertuples(index=False):
        origin_id = origin.functional_unit_id_main
        origin_node = f"SNAP::{origin_id}"
        lengths = nx.single_source_dijkstra_path_length(graph, origin_node, weight="weight") if origin_id in eligible_ids else {}
        distances, total_distances, redundant = [], [], 0
        for destination in core.itertuples(index=False):
            destination_id = destination.functional_unit_id_main
            destination_node = f"SNAP::{destination_id}"
            same = origin_id == destination_id
            reachable = int(destination_node in lengths)
            network_distance = float(lengths[destination_node]) if reachable else np.nan
            if same and reachable:
                total_distance = 0.0
            elif reachable:
                total_distance = network_distance + float(snap_lookup[origin_id]["snap_distance_km"]) + float(snap_lookup[destination_id]["snap_distance_km"])
            else:
                total_distance = np.nan
            direct = 0.0 if same else distance_km((origin.centroid_lon, origin.centroid_lat), (destination.centroid_lon, destination.centroid_lat))
            redundant_pair = int(not same and reachable and edge_component.get(origin_node) == edge_component.get(destination_node))
            pairwise.append({
                "network_period": "CONTEMPORARY",
                "network_variant": variant,
                "origin_id": origin_id,
                "origin_name": origin.canonical_city_name,
                "destination_id": destination_id,
                "destination_name": destination.canonical_city_name,
                "reachable": reachable,
                "network_distance_km": network_distance,
                "access_inclusive_distance_km": total_distance,
                "direct_geodesic_distance_km": direct,
                "detour_ratio": total_distance / direct if not same and reachable and direct > 0 else np.nan,
                "two_edge_disjoint_paths": redundant_pair,
            })
            if not same and reachable:
                distances.append(network_distance)
                total_distances.append(total_distance)
                redundant += redundant_pair
        snap_row = snap_lookup.get(origin_id, {})
        summaries.append({
            "functional_unit_id_main": origin_id,
            "source_core28_id": origin.source_core28_id,
            "canonical_city_name": origin.canonical_city_name,
            "network_period": "CONTEMPORARY",
            "network_variant": variant,
            "attached": int(origin_id in eligible_ids),
            "snap_distance_km": float(snap_row.get("snap_distance_km", np.nan)),
            "reachable_destinations": len(distances),
            "reachable_share": len(distances) / destination_denominator,
            "median_network_distance_km": float(np.median(distances)) if distances else np.nan,
            "mean_network_distance_km": float(np.mean(distances)) if distances else np.nan,
            "median_access_inclusive_distance_km": float(np.median(total_distances)) if total_distances else np.nan,
            "mean_access_inclusive_distance_km": float(np.mean(total_distances)) if total_distances else np.nan,
            "core_harmonic_access": float(sum(1.0 / value for value in total_distances if value > 0) / destination_denominator) if total_distances else 0.0,
            "core_betweenness_pair_count": float(betweenness.get(origin_node, 0.0)),
            "core_betweenness_share": float(betweenness.get(origin_node, 0.0) / denominator) if denominator else 0.0,
            "core_articulation_point": int(articulation.get(origin_node, 0)),
            "two_edge_redundant_destinations": redundant,
            "two_edge_redundancy_share": redundant / destination_denominator,
        })
    return pd.DataFrame(summaries), pd.DataFrame(pairwise)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config_path = resolve_project_path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output = resolve_project_path(config["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    radius_km = float(config["snap_radius_km"])

    coordinates = pd.read_csv(resolve_project_path(config["functional_units_csv"]))[["functional_unit_id_main", "centroid_lon", "centroid_lat"]].drop_duplicates("functional_unit_id_main")
    if config.get("city_universe", "CORE28") == "ALL148":
        identity = pd.read_csv(resolve_project_path(config["functional_units_csv"]))[["functional_unit_id_main", "functional_unit_name"]].drop_duplicates("functional_unit_id_main")
        identity = identity.rename(columns={"functional_unit_name": "canonical_city_name"})
        identity["source_core28_id"] = ""
        expected_cities = 148
    else:
        identity = pd.read_csv(resolve_project_path(config["core28_csv"]))[["functional_unit_id_main", "source_core28_id", "canonical_city_name"]].drop_duplicates("functional_unit_id_main")
        expected_cities = 28
    core = identity.merge(coordinates, on="functional_unit_id_main", how="left", validate="one_to_one").sort_values("functional_unit_id_main").reset_index(drop=True)
    if len(core) != expected_cities or core[["centroid_lon", "centroid_lat"]].isna().any().any():
        raise RuntimeError(f"Expected {expected_cities} unique cities with complete coordinates")

    all_classes = sorted({road_class for values in config["network_variants"].values() for road_class in values})
    where = "highway IN (" + ",".join(f"'{value}'" for value in all_classes) + ")"
    osm_path = resolve_project_path(config["osm_pbf"])
    roads_all = pyogrio.read_dataframe(osm_path, layer="lines", where=where, columns=["osm_id", "highway"])
    roads_all = roads_all[roads_all.geometry.notna() & ~roads_all.geometry.is_empty].copy().reset_index(drop=True)
    if roads_all.crs is None or roads_all.crs.to_epsg() != 4326:
        raise RuntimeError("Contemporary OSM roads must be EPSG:4326")

    all_summaries, all_pairs, snap_frames = [], [], []
    variant_diagnostics = []
    for variant, classes in config["network_variants"].items():
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Building {variant}", flush=True)
        roads = roads_all[roads_all.highway.isin(classes)].copy().reset_index(drop=True)
        graph = build_graph(roads)
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {variant}: graph {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges", flush=True)
        snaps = nearest_city_snaps(core, roads)
        graph, eligible = insert_city_nodes(graph, roads, snaps, radius_km)
        protected_nodes = {f"SNAP::{unit_id}" for unit_id in eligible.functional_unit_id_main}
        graph = retain_city_components_and_contract(graph, protected_nodes)
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {variant}: contracted to {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges", flush=True)
        summary, pairs = calculate_metrics(graph, core, eligible, variant)
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Completed {variant}", flush=True)
        all_summaries.append(summary)
        all_pairs.append(pairs)
        snaps["network_variant"] = variant
        snaps["attached"] = snaps.snap_distance_km.le(radius_km).astype(int)
        snap_frames.append(snaps)
        variant_diagnostics.append({
            "network_variant": variant,
            "road_features": len(roads),
            "graph_nodes": graph.number_of_nodes(),
            "graph_edges": graph.number_of_edges(),
            "connected_components": nx.number_connected_components(graph),
            "attached_cities": int(summary.attached.sum()),
            "reachable_ordered_nonself_pairs": int(pairs.loc[pairs.origin_id.ne(pairs.destination_id), "reachable"].sum()),
        })

    summary = pd.concat(all_summaries, ignore_index=True)
    pairs = pd.concat(all_pairs, ignore_index=True)
    snaps = pd.concat(snap_frames, ignore_index=True)
    diagnostics = pd.DataFrame(variant_diagnostics)
    summary.to_csv(output / "CORE28_CONTEMPORARY_NETWORK_SUMMARY.csv", index=False)
    pairs.to_csv(output / "CORE28_CONTEMPORARY_PAIRWISE_DISTANCES.csv", index=False)
    snaps.to_csv(output / "CORE28_CONTEMPORARY_CITY_SNAPS.csv", index=False)
    diagnostics.to_csv(output / "CORE28_CONTEMPORARY_NETWORK_DIAGNOSTICS.csv", index=False)

    comparison = pd.DataFrame()
    correlations = pd.DataFrame()
    if expected_cities == 28:
        historical = pd.read_csv(resolve_project_path(config["historical_summary_csv"]))
        historical = historical[(historical.network_variant == "ALL_MAPPED_STATUSES") & (historical.snap_radius_km == radius_km)].copy()
        structural = summary[summary.network_variant.eq("STRUCTURAL_ROADS")].copy()
        comparison_rows = []
        for year in sorted(historical.map_year.unique()):
            left = historical[historical.map_year.eq(year)].copy()
            merged = left.merge(structural, on=["functional_unit_id_main", "source_core28_id", "canonical_city_name"], suffixes=("_historical", "_contemporary"), validate="one_to_one")
            for measure in ["reachable_share", "core_harmonic_access", "core_betweenness_share", "core_articulation_point"]:
                h = merged[f"{measure}_historical"]
                c = merged[f"{measure}_contemporary"]
                merged[f"{measure}_historical_rank"] = h.rank(method="average", pct=True)
                merged[f"{measure}_contemporary_rank"] = c.rank(method="average", pct=True)
                merged[f"{measure}_rank_change"] = merged[f"{measure}_contemporary_rank"] - merged[f"{measure}_historical_rank"]
            merged["historical_year"] = int(year)
            comparison_rows.append(merged)
        comparison = pd.concat(comparison_rows, ignore_index=True)
        comparison.to_csv(output / "CORE28_HISTORICAL_CONTEMPORARY_COMPARISON.csv", index=False)

        correlation_rows = []
        for year in sorted(comparison.historical_year.unique()):
            frame = comparison[comparison.historical_year.eq(year)]
            for measure in ["reachable_share", "core_harmonic_access", "core_betweenness_share", "core_articulation_point"]:
                correlation_rows.append({
                    "historical_year": int(year),
                    "measure": measure,
                    "cities": len(frame),
                    "spearman_rank_correlation": frame[f"{measure}_historical"].corr(frame[f"{measure}_contemporary"], method="spearman"),
                    "pearson_correlation": frame[f"{measure}_historical"].corr(frame[f"{measure}_contemporary"], method="pearson"),
                })
        correlations = pd.DataFrame(correlation_rows)
        correlations.to_csv(output / "CORE28_HISTORICAL_CONTEMPORARY_CORRELATIONS.csv", index=False)

    validation = pd.DataFrame([
        {"check": "EXPECTED_CITIES_PER_VARIANT", "status": "PASS" if summary.groupby("network_variant").size().eq(expected_cities).all() else "FAIL"},
        {"check": "ALL_CITIES_ATTACHED_WITHIN_10KM", "status": "PASS" if summary.attached.eq(1).all() else "FAIL"},
        {"check": "PAIRWISE_SYMMETRY", "status": "PASS" if all(np.allclose(group.network_distance_km.to_numpy().reshape(expected_cities, expected_cities), group.network_distance_km.to_numpy().reshape(expected_cities, expected_cities).T, equal_nan=True) for _, group in pairs.groupby("network_variant", sort=False)) else "FAIL"},
        {"check": "NONNEGATIVE_DISTANCES", "status": "PASS" if pairs.network_distance_km.dropna().ge(0).all() else "FAIL"},
        {"check": "NETWORK_PATH_DOMINATES_GEODESIC", "status": "PASS" if pairs.detour_ratio.dropna().ge(1 - 1e-8).all() else "FAIL"},
        {"check": "HISTORICAL_COMPARISON_SCOPE", "status": "PASS" if (len(comparison) == 56 if expected_cities == 28 else len(comparison) == 0) else "FAIL"},
    ])
    validation.to_csv(output / "VALIDATION.csv", index=False)
    if validation.status.eq("FAIL").any():
        raise RuntimeError("Validation failed: " + ", ".join(validation.loc[validation.status.eq("FAIL"), "check"]))

    gpkg = output / "CORE28_CONTEMPORARY_NETWORK_METRICS.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    city_points = summary.merge(core[["functional_unit_id_main", "centroid_lon", "centroid_lat"]], on="functional_unit_id_main", how="left", validate="many_to_one")
    city_points = gpd.GeoDataFrame(city_points, geometry=[Point(x, y) for x, y in zip(city_points.centroid_lon, city_points.centroid_lat)], crs="EPSG:4326")
    pyogrio.write_dataframe(city_points, gpkg, layer="core28_current_metrics", driver="GPKG")
    snap_points = gpd.GeoDataFrame(snaps.copy(), geometry=[Point(x, y) for x, y in zip(snaps.snap_lon, snaps.snap_lat)], crs="EPSG:4326")
    pyogrio.write_dataframe(snap_points, gpkg, layer="core28_current_snaps", driver="GPKG", append=True)
    if not comparison.empty:
        comparison_points = comparison.merge(core[["functional_unit_id_main", "centroid_lon", "centroid_lat"]], on="functional_unit_id_main", how="left")
        comparison_points = gpd.GeoDataFrame(comparison_points, geometry=[Point(x, y) for x, y in zip(comparison_points.centroid_lon, comparison_points.centroid_lat)], crs="EPSG:4326")
        pyogrio.write_dataframe(comparison_points, gpkg, layer="historical_current_comparison", driver="GPKG", append=True)
    with sqlite3.connect(gpkg) as connection:
        pairs.to_sql("core28_current_pairwise", connection, if_exists="replace", index=False)
        if not correlations.empty:
            correlations.to_sql("historical_current_correlations", connection, if_exists="replace", index=False)
        diagnostics.to_sql("network_diagnostics", connection, if_exists="replace", index=False)
        validation.to_sql("validation", connection, if_exists="replace", index=False)

    receipt = {
        "generated_at": datetime.now(ZoneInfo("Africa/Casablanca")).isoformat(timespec="seconds"),
        "status": "TECHNICALLY_VERIFIED_CONTEMPORARY_COMPARATOR_NOT_AN_INSTRUMENT",
        "osm_source": str(osm_path),
        "osm_sha256": sha256(osm_path),
        "snap_radius_km": radius_km,
        "network_variants": config["network_variants"],
        "redundancy_definition": "Share of the other 27 Core-28 cities connected by at least two edge-disjoint road paths; unreachable cities contribute zero.",
        "nonclaim": "The contemporary network is post-treatment and is not promoted as an excluded instrument.",
    }
    (output / "RECEIPT.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote contemporary metrics for {len(summary)} city-variant rows to {output}")


if __name__ == "__main__":
    main()
