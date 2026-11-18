#!/usr/bin/env python3
"""Build bounded Core-28 historical network-distance measures for 1922 and 1925."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
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
from pyproj import Geod
from shapely.geometry import LineString, Point
from shapely.ops import substring


GEOD = Geod(ellps="WGS84")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def geodesic_length_km(geometry) -> float:
    return abs(float(GEOD.geometry_length(geometry))) / 1000.0


def point_distance_km(left: Point, right: Point) -> float:
    return abs(float(GEOD.inv(left.x, left.y, right.x, right.y)[2])) / 1000.0


def write_layer(frame: gpd.GeoDataFrame, path: Path, layer: str) -> None:
    pyogrio.write_dataframe(frame, path, layer=layer, driver="GPKG", append=path.exists())


def nearest_snap(city: Point, edges: gpd.GeoDataFrame):
    best = None
    for idx, geometry in zip(edges.index, edges.geometry):
        position = float(geometry.project(city))
        snap = geometry.interpolate(position)
        distance = point_distance_km(city, snap)
        if best is None or distance < best[0]:
            best = (distance, idx, position, snap)
    return best


def split_graph(edges: gpd.GeoDataFrame, eligible: pd.DataFrame):
    graph = nx.Graph()
    snaps_by_edge: dict[int, list[dict]] = {}
    for row in eligible.to_dict("records"):
        snaps_by_edge.setdefault(int(row["edge_index"]), []).append(row)

    for edge_index, edge in edges.iterrows():
        geometry = edge.geometry
        from_node = str(edge.from_node)
        to_node = str(edge.to_node)
        edge_snaps = sorted(snaps_by_edge.get(int(edge_index), []), key=lambda x: float(x["projected_position"]))
        chain = [(0.0, from_node)]
        for item in edge_snaps:
            chain.append((float(item["projected_position"]), f"SNAP::{item['functional_unit_id_main']}"))
        chain.append((float(geometry.length), to_node))
        deduplicated = []
        for position, node in chain:
            if deduplicated and math.isclose(position, deduplicated[-1][0], abs_tol=1e-12):
                graph.add_edge(deduplicated[-1][1], node, weight=0.0)
            else:
                deduplicated.append((position, node))
        for (start, left), (end, right) in zip(deduplicated[:-1], deduplicated[1:]):
            segment = substring(geometry, start, end)
            weight = geodesic_length_km(segment)
            if graph.has_edge(left, right):
                graph[left][right]["weight"] = min(graph[left][right]["weight"], weight)
            else:
                graph.add_edge(left, right, weight=weight)
    return graph


def reachable_core_pair_count(graph: nx.Graph, core_nodes: list[str]) -> int:
    core_set = set(core_nodes)
    total = 0
    for component in nx.connected_components(graph):
        count = len(core_set.intersection(component))
        total += count * (count - 1) // 2
    return total


def core_articulation_flags(graph: nx.Graph, core_nodes: list[str]) -> dict[str, int]:
    flags = {}
    for node in core_nodes:
        others = [value for value in core_nodes if value != node]
        baseline = reachable_core_pair_count(graph, others)
        reduced = graph.copy()
        reduced.remove_node(node)
        flags[node] = int(reachable_core_pair_count(reduced, others) < baseline)
    return flags


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(resolve_project_path(args.config).read_text(encoding="utf-8"))
    output = resolve_project_path(config["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    years = [int(value) for value in config["years"]]
    radii = [float(value) for value in config["snap_radii_km"]]
    variants = [str(value) for value in config["variants"]]
    primary_radius = float(config["primary_snap_km"])

    identity = pd.read_csv(resolve_project_path(config["core28_csv"]))[["functional_unit_id_main", "source_core28_id", "canonical_city_name"]].drop_duplicates("functional_unit_id_main")
    coordinates = pd.read_csv(resolve_project_path(config["functional_units_csv"]))[["functional_unit_id_main", "centroid_lon", "centroid_lat"]].drop_duplicates("functional_unit_id_main")
    core = identity.merge(coordinates, on="functional_unit_id_main", how="left", validate="one_to_one")
    if len(core) != 28 or core[["centroid_lon", "centroid_lat"]].isna().any().any():
        raise RuntimeError("Expected 28 unique Core-28 cities with complete coordinates")
    core = core.sort_values("functional_unit_id_main").reset_index(drop=True)

    summaries = []
    pairwise = []
    snap_rows = []
    source_hashes = {}
    control_edges = {}
    control_snaps = {}
    control_layer_names = {}

    for year in years:
        topology_path = resolve_project_path(config["topology_gpkg"][str(year)])
        source_hashes[str(year)] = sha256(topology_path)
        layer = config.get("topology_layer", {}).get(str(year), "routes_topologiques_strictes_geographiques")
        control_layer_names[year] = layer
        all_edges = pyogrio.read_dataframe(topology_path, layer=layer)
        if layer == "routes_topologiques_strictes_geographiques":
            all_edges = all_edges[(all_edges.tolerance_px == 0.0) & (all_edges.graph_variant == "STRICT")].copy().reset_index(drop=True)
        else:
            all_edges = all_edges.copy().reset_index(drop=True)
        if all_edges.crs is None or all_edges.crs.to_epsg() != 4326:
            raise RuntimeError(f"C{year}: expected EPSG:4326 topological edges")

        for variant in variants:
            if variant == "ALL_MAPPED_STATUSES":
                edges = all_edges.copy().reset_index(drop=True)
            elif variant == "OBSERVED_FRAGMENTS_ONLY":
                edges = all_edges[all_edges.source_role.eq("V1_FRAGMENT")].copy().reset_index(drop=True)
            else:
                raise RuntimeError(f"Unknown network variant: {variant}")
            if edges.empty:
                raise RuntimeError(f"C{year} {variant}: no edges")

            snap_edges = edges[edges.source_role.ne("TOPOLOGY_T_JUNCTION_REPAIR")].copy()
            nearest = []
            for city in core.itertuples(index=False):
                city_point = Point(float(city.centroid_lon), float(city.centroid_lat))
                distance, edge_index, position, snap = nearest_snap(city_point, snap_edges)
                nearest.append({
                    "functional_unit_id_main": city.functional_unit_id_main,
                    "source_core28_id": city.source_core28_id,
                    "canonical_city_name": city.canonical_city_name,
                    "map_year": year,
                    "network_variant": variant,
                    "nearest_edge_id": str(edges.loc[edge_index, "topo_edge_id"]),
                    "edge_index": int(edge_index),
                    "projected_position": float(position),
                    "snap_distance_km": float(distance),
                    "city_lon": float(city.centroid_lon),
                    "city_lat": float(city.centroid_lat),
                    "snap_lon": float(snap.x),
                    "snap_lat": float(snap.y),
                })
            nearest_frame = pd.DataFrame(nearest)

            for radius in radii:
                eligible = nearest_frame[nearest_frame.snap_distance_km <= radius + 1e-12].copy()
                graph = split_graph(edges, eligible)
                eligible_ids = set(eligible.functional_unit_id_main)
                snap_lookup = eligible.set_index("functional_unit_id_main").to_dict("index")
                core_snap_nodes = [f"SNAP::{unit_id}" for unit_id in sorted(eligible_ids)]
                if len(core_snap_nodes) > 1:
                    core_betweenness = nx.betweenness_centrality_subset(graph, sources=core_snap_nodes, targets=core_snap_nodes, normalized=False, weight="weight")
                    core_articulation = core_articulation_flags(graph, core_snap_nodes)
                else:
                    core_betweenness = {}
                    core_articulation = {}
                for row in nearest_frame.to_dict("records"):
                    snap_rows.append({**row, "snap_radius_km": radius, "attached": int(row["functional_unit_id_main"] in eligible_ids)})

                for origin in core.itertuples(index=False):
                    origin_id = origin.functional_unit_id_main
                    distances = []
                    for destination in core.itertuples(index=False):
                        destination_id = destination.functional_unit_id_main
                        same = origin_id == destination_id
                        attached = origin_id in eligible_ids and destination_id in eligible_ids
                        network_distance = np.nan
                        total_distance = np.nan
                        direct_distance = point_distance_km(Point(float(origin.centroid_lon), float(origin.centroid_lat)), Point(float(destination.centroid_lon), float(destination.centroid_lat))) if not same else 0.0
                        detour_ratio = np.nan
                        reachable = 0
                        if same and origin_id in eligible_ids:
                            network_distance = 0.0
                            total_distance = 0.0
                            reachable = 1
                        elif attached:
                            left = f"SNAP::{origin_id}"
                            right = f"SNAP::{destination_id}"
                            try:
                                network_distance = float(nx.shortest_path_length(graph, left, right, weight="weight"))
                                total_distance = network_distance + float(snap_lookup[origin_id]["snap_distance_km"]) + float(snap_lookup[destination_id]["snap_distance_km"])
                                detour_ratio = total_distance / direct_distance if direct_distance > 0 else np.nan
                                reachable = 1
                            except nx.NetworkXNoPath:
                                pass
                        pairwise.append({
                            "map_year": year,
                            "network_variant": variant,
                            "snap_radius_km": radius,
                            "origin_id": origin_id,
                            "origin_name": origin.canonical_city_name,
                            "destination_id": destination_id,
                            "destination_name": destination.canonical_city_name,
                            "origin_attached": int(origin_id in eligible_ids),
                            "destination_attached": int(destination_id in eligible_ids),
                            "reachable": reachable,
                            "network_distance_km": network_distance,
                            "access_inclusive_distance_km": total_distance,
                            "direct_geodesic_distance_km": direct_distance,
                            "detour_ratio": detour_ratio,
                        })
                        if not same and reachable:
                            distances.append((network_distance, total_distance))
                    origin_snap = nearest_frame[nearest_frame.functional_unit_id_main.eq(origin_id)].iloc[0]
                    network_values = [value[0] for value in distances]
                    total_values = [value[1] for value in distances]
                    summaries.append({
                        "functional_unit_id_main": origin_id,
                        "source_core28_id": origin.source_core28_id,
                        "canonical_city_name": origin.canonical_city_name,
                        "map_year": year,
                        "network_variant": variant,
                        "snap_radius_km": radius,
                        "primary_snap_radius": int(math.isclose(radius, primary_radius)),
                        "attached": int(origin_id in eligible_ids),
                        "snap_distance_km": float(origin_snap.snap_distance_km),
                        "reachable_destinations": len(distances),
                        "reachable_share": len(distances) / 27.0,
                        "median_network_distance_km": float(np.median(network_values)) if network_values else np.nan,
                        "mean_network_distance_km": float(np.mean(network_values)) if network_values else np.nan,
                        "median_access_inclusive_distance_km": float(np.median(total_values)) if total_values else np.nan,
                        "mean_access_inclusive_distance_km": float(np.mean(total_values)) if total_values else np.nan,
                        "core_harmonic_access": float(sum(1.0 / value for value in total_values if value > 0) / 27.0) if total_values else 0.0,
                        "core_betweenness_pair_count": float(core_betweenness.get(f"SNAP::{origin_id}", 0.0)),
                        "core_betweenness_share": float(core_betweenness.get(f"SNAP::{origin_id}", 0.0) / ((len(core_snap_nodes) - 1) * (len(core_snap_nodes) - 2) / 2.0)) if len(core_snap_nodes) > 2 else 0.0,
                        "core_articulation_point": int(core_articulation.get(f"SNAP::{origin_id}", 0)),
                        "measurement_status": "NETWORK_MEASUREMENT_NOT_HISTORICAL_CONNECTION_PROOF",
                    })

                if math.isclose(radius, primary_radius) and variant == "ALL_MAPPED_STATUSES":
                    control_edges[year] = edges
                    snap_geo = nearest_frame.copy()
                    snap_geo["attached"] = snap_geo.snap_distance_km.le(radius).astype(int)
                    snap_geo["geometry"] = [Point(x, y) for x, y in zip(snap_geo.snap_lon, snap_geo.snap_lat)]
                    control_snaps[year] = gpd.GeoDataFrame(snap_geo, geometry="geometry", crs="EPSG:4326")

    summary = pd.DataFrame(summaries).sort_values(["map_year", "network_variant", "snap_radius_km", "functional_unit_id_main"])
    pairs = pd.DataFrame(pairwise).sort_values(["map_year", "network_variant", "snap_radius_km", "origin_id", "destination_id"])
    snaps = pd.DataFrame(snap_rows).sort_values(["map_year", "network_variant", "snap_radius_km", "functional_unit_id_main"])
    summary.to_csv(output / "CORE28_NETWORK_ACCESS_SUMMARY.csv", index=False)
    pairs.to_csv(output / "CORE28_PAIRWISE_NETWORK_DISTANCES.csv", index=False)
    snaps.to_csv(output / "CORE28_CITY_SNAP_DIAGNOSTICS.csv", index=False)

    validation = []
    def check(name: str, condition: bool, detail: str = "") -> None:
        validation.append({"check": name, "status": "PASS" if condition else "FAIL", "detail": detail})

    check("EXPECTED_SUMMARY_ROWS", len(summary) == 28 * len(years) * len(radii) * len(variants), str(len(summary)))
    check("EXPECTED_PAIRWISE_ROWS", len(pairs) == 28 * 28 * len(years) * len(radii) * len(variants), str(len(pairs)))
    check("UNIQUE_SUMMARY_KEY", not summary.duplicated(["functional_unit_id_main", "map_year", "network_variant", "snap_radius_km"]).any())
    check("UNIQUE_PAIRWISE_KEY", not pairs.duplicated(["origin_id", "destination_id", "map_year", "network_variant", "snap_radius_km"]).any())
    check("NONNEGATIVE_DISTANCES", bool((pairs.network_distance_km.dropna() >= -1e-9).all() and (pairs.access_inclusive_distance_km.dropna() >= -1e-9).all()))
    check("ACCESS_INCLUSIVE_DOMINATES_NETWORK", bool(((pairs.access_inclusive_distance_km - pairs.network_distance_km).dropna() >= -1e-8).all()))
    check("NETWORK_PATH_DOMINATES_DIRECT_DISTANCE", bool((pairs.detour_ratio.dropna() >= 1 - 1e-8).all()), f"min_ratio={pairs.detour_ratio.dropna().min() if pairs.detour_ratio.notna().any() else 'NA'}")
    diagonal = pairs[pairs.origin_id.eq(pairs.destination_id) & pairs.reachable.eq(1)]
    check("REACHABLE_DIAGONAL_ZERO", bool((diagonal.network_distance_km.abs() <= 1e-9).all() and (diagonal.access_inclusive_distance_km.abs() <= 1e-9).all()))
    symmetry = pairs.merge(pairs, left_on=["map_year", "network_variant", "snap_radius_km", "origin_id", "destination_id"], right_on=["map_year", "network_variant", "snap_radius_km", "destination_id", "origin_id"], suffixes=("_a", "_b"))
    symdiff = (symmetry.network_distance_km_a - symmetry.network_distance_km_b).abs().dropna()
    check("PAIRWISE_SYMMETRY", bool((symdiff <= 1e-7).all()), f"max_diff={symdiff.max() if len(symdiff) else 0}")
    monotonic = summary.groupby(["functional_unit_id_main", "map_year", "network_variant"], sort=False).apply(lambda frame: frame.sort_values("snap_radius_km").attached.is_monotonic_increasing, include_groups=False)
    check("ATTACHMENT_MONOTONIC_IN_RADIUS", bool(monotonic.all()))
    reach = summary.pivot_table(index=["functional_unit_id_main", "map_year", "snap_radius_km"], columns="network_variant", values="reachable_destinations", aggfunc="first")
    check("OBSERVED_ONLY_NOT_MORE_REACHABLE", bool((reach["OBSERVED_FRAGMENTS_ONLY"] <= reach["ALL_MAPPED_STATUSES"]).all()))
    check("SOURCE_HASHES_COMPLETE", len(source_hashes) == len(years))
    validation_frame = pd.DataFrame(validation)
    validation_frame.to_csv(output / "CORE28_ROUTABLE_NETWORK_VALIDATION.csv", index=False)
    if validation_frame.status.ne("PASS").any():
        raise RuntimeError("Routable-network validation failed: " + ", ".join(validation_frame.loc[validation_frame.status.ne("PASS"), "check"]))

    gpkg = output / "CORE28_ROUTABLE_NETWORK_CONTROL.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    city_points = gpd.GeoDataFrame(core.copy(), geometry=[Point(x, y) for x, y in zip(core.centroid_lon, core.centroid_lat)], crs="EPSG:4326")
    write_layer(city_points, gpkg, "core28_cities")
    for year in years:
        write_layer(control_edges[year], gpkg, f"network_edges_{year}")
        write_layer(control_snaps[year], gpkg, f"city_snaps_{year}")

    fig, axes = plt.subplots(1, len(years), figsize=(20, 11), constrained_layout=True)
    if len(years) == 1:
        axes = [axes]
    for ax, year in zip(axes, years):
        ordinary = control_edges[year][control_edges[year].source_role.ne("TOPOLOGY_T_JUNCTION_REPAIR")]
        repairs = control_edges[year][control_edges[year].source_role.eq("TOPOLOGY_T_JUNCTION_REPAIR")]
        ordinary.plot(ax=ax, color="#8c8c8c", linewidth=0.45, alpha=0.75)
        if not repairs.empty:
            repairs.plot(ax=ax, color="#d73027", linewidth=0.9, alpha=0.9, label="Raccord topologique")
        snap = control_snaps[year]
        attached = snap[snap.attached.eq(1)]
        unattached = snap[snap.attached.eq(0)]
        if not attached.empty:
            attached.plot(ax=ax, color="#1b7837", markersize=28, zorder=3, label="Rattachée à 10 km")
        if not unattached.empty:
            unattached.plot(ax=ax, color="#b2182b", markersize=36, marker="x", zorder=4, label="Non rattachée")
        for row in snap.itertuples(index=False):
            ax.plot([row.city_lon, row.snap_lon], [row.city_lat, row.snap_lat], color="#2166ac", linewidth=0.55, alpha=0.8)
            ax.annotate(str(row.canonical_city_name), (row.snap_lon, row.snap_lat), xytext=(2, 2), textcoords="offset points", fontsize=5, color="#202020")
        topology_label = "topologie réparée issue de la carte" if control_layer_names[year] == "repaired_edges_geographic" else "topologie stricte exacte"
        ax.set_title(f"{year} — {topology_label}, tous statuts")
        ax.set_axis_off()
        ax.legend(loc="lower left", frameon=True)
    fig.suptitle("Rattachement Core-28 au réseau historique — rayon principal 10 km", fontsize=17)
    fig.savefig(output / "CORE28_ROUTABLE_NETWORK_CONTROL_HIGHRES.png", dpi=300, bbox_inches="tight")
    plt.close(fig)

    receipt = {
        "generated_at": datetime.now(ZoneInfo("Africa/Casablanca")).isoformat(timespec="seconds"),
        "status": "NETWORK_MEASUREMENT_COMPLETE_NOT_HISTORICAL_CONNECTION_PROOF_NOT_ECONOMETRIC_ESTIMATION",
        "years": years,
        "snap_radii_km": radii,
        "primary_snap_km": primary_radius,
        "variants": variants,
        "source_topology_sha256": source_hashes,
        "summary_rows": len(summary),
        "pairwise_rows": len(pairs),
        "definition": "Shortest-path length on the configured map-derived topological edges; primary access-inclusive distance adds straight centroid-to-snap distance at both endpoints.",
        "nonclaim": "A snap is a computational attachment, not proof of historical connection; a missing path is not proof of historical disconnection.",
    }
    (output / "CORE28_ROUTABLE_NETWORK_RECEIPT.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(summary)} city summaries and {len(pairs)} pairwise rows to {output}")


if __name__ == "__main__":
    main()
