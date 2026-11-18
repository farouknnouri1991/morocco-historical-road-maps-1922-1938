#!/usr/bin/env python3
"""Add the same edge-disjoint-path redundancy measure to the 1922/1925 Core-28 graphs."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pyogrio
from shapely.geometry import Point
from path_utils import resolve_project_path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    config = json.loads((ROOT / "config" / "core28_measurement_from_routable_maps.json").read_text(encoding="utf-8"))
    historical_tools = load_module("historical_tools", ROOT / "code" / "50_build_core28_routable_network.py")
    current_tools = load_module("current_tools", ROOT / "code" / "54_build_contemporary_core28_network.py")
    output = ROOT / "outputs" / "contemporary_core28_network_2026-10-11_v1"

    identity = pd.read_csv(resolve_project_path(config["core28_csv"]))[["functional_unit_id_main", "source_core28_id", "canonical_city_name"]].drop_duplicates("functional_unit_id_main")
    coordinates = pd.read_csv(resolve_project_path(config["functional_units_csv"]))[["functional_unit_id_main", "centroid_lon", "centroid_lat"]].drop_duplicates("functional_unit_id_main")
    core = identity.merge(coordinates, on="functional_unit_id_main", how="left", validate="one_to_one").sort_values("functional_unit_id_main").reset_index(drop=True)
    current = pd.read_csv(output / "CORE28_CONTEMPORARY_NETWORK_SUMMARY.csv")
    current = current[current.network_variant.eq("STRUCTURAL_ROADS")][["functional_unit_id_main", "two_edge_redundancy_share"]].rename(columns={"two_edge_redundancy_share": "two_edge_redundancy_share_contemporary"})

    rows = []
    for year in [1922, 1925]:
        path = resolve_project_path(config["topology_gpkg"][str(year)])
        layer = config["topology_layer"][str(year)]
        edges = pyogrio.read_dataframe(path, layer=layer).reset_index(drop=True)
        snap_edges = edges[edges.source_role.ne("TOPOLOGY_T_JUNCTION_REPAIR")].copy()
        nearest = []
        for city in core.itertuples(index=False):
            city_point = Point(float(city.centroid_lon), float(city.centroid_lat))
            distance, edge_index, position, snap = historical_tools.nearest_snap(city_point, snap_edges)
            nearest.append({
                "functional_unit_id_main": city.functional_unit_id_main,
                "edge_index": int(edge_index),
                "projected_position": float(position),
                "snap_distance_km": float(distance),
            })
        eligible = pd.DataFrame(nearest)
        eligible = eligible[eligible.snap_distance_km <= 10.0].copy()
        graph = historical_tools.split_graph(edges, eligible)
        lookup = current_tools.two_edge_component_lookup(graph)
        eligible_ids = set(eligible.functional_unit_id_main)
        for city in core.itertuples(index=False):
            origin = f"SNAP::{city.functional_unit_id_main}"
            count = 0
            if city.functional_unit_id_main in eligible_ids:
                for destination in core.functional_unit_id_main:
                    if destination == city.functional_unit_id_main or destination not in eligible_ids:
                        continue
                    target = f"SNAP::{destination}"
                    count += int(lookup.get(origin) == lookup.get(target))
            rows.append({
                "functional_unit_id_main": city.functional_unit_id_main,
                "canonical_city_name": city.canonical_city_name,
                "historical_year": year,
                "two_edge_redundant_destinations": count,
                "two_edge_redundancy_share_historical": count / 27.0,
            })
    historical = pd.DataFrame(rows)
    comparison = historical.merge(current, on="functional_unit_id_main", how="left", validate="many_to_one")
    comparison["historical_rank"] = comparison.groupby("historical_year")["two_edge_redundancy_share_historical"].rank(method="average", pct=True)
    comparison["contemporary_rank"] = comparison.groupby("historical_year")["two_edge_redundancy_share_contemporary"].rank(method="average", pct=True)
    comparison["rank_change"] = comparison.contemporary_rank - comparison.historical_rank
    comparison.to_csv(output / "CORE28_HISTORICAL_CONTEMPORARY_REDUNDANCY.csv", index=False)
    correlations = comparison.groupby("historical_year").apply(
        lambda frame: pd.Series({
            "spearman_rank_correlation": frame.two_edge_redundancy_share_historical.corr(frame.two_edge_redundancy_share_contemporary, method="spearman"),
            "pearson_correlation": frame.two_edge_redundancy_share_historical.corr(frame.two_edge_redundancy_share_contemporary, method="pearson"),
        }), include_groups=False
    ).reset_index()
    correlations.to_csv(output / "CORE28_HISTORICAL_CONTEMPORARY_REDUNDANCY_CORRELATIONS.csv", index=False)
    print(comparison.groupby("historical_year").two_edge_redundancy_share_historical.agg(["min", "max", "nunique"]))
    print(correlations)


if __name__ == "__main__":
    main()
