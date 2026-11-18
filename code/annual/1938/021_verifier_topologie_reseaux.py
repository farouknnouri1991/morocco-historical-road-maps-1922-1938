#!/usr/bin/env python3
"""Vérification indépendante des GeoPackages topologiques dérivés des six réseaux existants."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio


ROOT = Path(__file__).resolve().parents[2]
PRODUCTS = {
    1922: "C1922_PRODUIT_FINAL_CONSOLIDE.gpkg",
    1925: "C1925_PRODUIT_CONSOLIDE_ESSAI.gpkg",
    1928: "C1928_PRODUIT_FINAL_CONSOLIDE.gpkg",
    1934: "C1934_PRODUIT_FINAL_CONSOLIDE.gpkg",
    1936: "C1936_PRODUIT_FINAL_CONSOLIDE.gpkg",
    1938: "C1938_PRODUIT_FINAL_CONSOLIDE.gpkg",
}
REQUIRED = {
    "routes_sources_carte", "routes_sources_geographiques",
    "routes_topologiques_strictes", "routes_topologiques_larges",
    "routes_topologiques_strictes_geographiques", "routes_topologiques_larges_geographiques",
    "noeuds_topologiques", "noeuds_topologiques_geographiques",
    "corridors_continus", "corridors_continus_geographiques",
    "composantes_connexes", "composantes_connexes_geographiques",
    "croisements_ambigus", "terminaisons_reseau_a_interpreter", "lineage_edges_sources",
}
REQUIRED_VIEWS = {
    "vue_routes_strictes_exactes", "vue_routes_larges_1px",
    "vue_routes_strictes_exactes_geo", "vue_routes_larges_1px_geo",
    "vue_noeuds_stricts_exacts", "vue_noeuds_stricts_exacts_geo",
    "vue_corridors_stricts_exacts",
}


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


def read_table(path: Path, layer: str) -> pd.DataFrame:
    return pyogrio.read_dataframe(path, layer=layer)


def assert_source_copy(source: gpd.GeoDataFrame, preserved: gpd.GeoDataFrame, label: str) -> None:
    if set(source.columns) != set(preserved.columns):
        raise RuntimeError(f"{label}:SOURCE_COLUMNS_CHANGED")
    source_ordered = source.sort_values("edge_id").reset_index(drop=True)
    preserved_ordered = preserved.sort_values("edge_id").reset_index(drop=True)
    if list(source_ordered.geometry.to_wkb(hex=True)) != list(preserved_ordered.geometry.to_wkb(hex=True)):
        raise RuntimeError(f"{label}:SOURCE_GEOMETRY_CHANGED")
    columns = sorted(column for column in source.columns if column != source.geometry.name)
    try:
        pd.testing.assert_frame_equal(
            source_ordered[columns], preserved_ordered[columns], check_dtype=False, check_like=True
        )
    except AssertionError as error:
        raise RuntimeError(f"{label}:SOURCE_ATTRIBUTES_CHANGED") from error


def verify_year(year: int) -> dict[str, object]:
    directory = ROOT / f"REPRODUCTION_{year}" / "outputs"
    source = directory / PRODUCTS[year]
    product = directory / f"C{year}_RESEAU_TOPOLOGIQUE.gpkg"
    control_path = directory / f"C{year}_RESEAU_TOPOLOGIQUE.controle.json"
    control = json.loads(control_path.read_text(encoding="utf-8"))
    with sqlite3.connect(product) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        registered = {row[0] for row in connection.execute("SELECT table_name FROM gpkg_contents")}
        sqlite_views = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'view'")}
    if integrity != "ok" or not REQUIRED.issubset(registered) or not REQUIRED_VIEWS.issubset(registered & sqlite_views):
        raise RuntimeError(f"C{year}:GPKG_STRUCTURE_FAILED")
    if sha256(source) != control["source_sha256"]:
        raise RuntimeError(f"C{year}:SOURCE_CHANGED_AFTER_TOPOLOGY")
    source_map = gpd.read_file(source, layer=f"routes_{year}_carte")
    source_geo = gpd.read_file(source, layer=f"routes_{year}_geographiques")
    preserved_map = gpd.read_file(product, layer="routes_sources_carte")
    preserved_geo = gpd.read_file(product, layer="routes_sources_geographiques")
    assert_source_copy(source_map, preserved_map, f"C{year}:MAP")
    assert_source_copy(source_geo, preserved_geo, f"C{year}:GEO")
    nodes = gpd.read_file(product, layer="noeuds_topologiques")
    geo_nodes = gpd.read_file(product, layer="noeuds_topologiques_geographiques")
    corridors = gpd.read_file(product, layer="corridors_continus")
    geo_corridors = gpd.read_file(product, layer="corridors_continus_geographiques")
    components = gpd.read_file(product, layer="composantes_connexes")
    geo_components = gpd.read_file(product, layer="composantes_connexes_geographiques")
    lineage = read_table(product, "lineage_edges_sources")
    mode_checks: list[dict] = []
    source_ids = set(source_map.edge_id.astype(str))
    source_length = float(source_map.geometry.length.sum())
    for tolerance in (0.0, 0.5, 1.0):
        for variant, layer in (("STRICT", "routes_topologiques_strictes"), ("LARGE", "routes_topologiques_larges")):
            edges = gpd.read_file(product, layer=layer)
            edges = edges[(edges.tolerance_px == tolerance) & edges.graph_variant.eq(variant)].copy()
            geo_layer = layer + "_geographiques"
            geo_edges = gpd.read_file(product, layer=geo_layer)
            geo_edges = geo_edges[(geo_edges.tolerance_px == tolerance) & geo_edges.graph_variant.eq(variant)].copy()
            mode_nodes = nodes[(nodes.tolerance_px == tolerance) & nodes.graph_variant.eq(variant)].copy()
            mode_geo_nodes = geo_nodes[(geo_nodes.tolerance_px == tolerance) & geo_nodes.graph_variant.eq(variant)].copy()
            mode_corridors = corridors[(corridors.tolerance_px == tolerance) & corridors.graph_variant.eq(variant)].copy()
            mode_geo_corridors = geo_corridors[(geo_corridors.tolerance_px == tolerance) & geo_corridors.graph_variant.eq(variant)].copy()
            mode_components = components[(components.tolerance_px == tolerance) & components.graph_variant.eq(variant)].copy()
            mode_geo_components = geo_components[(geo_components.tolerance_px == tolerance) & geo_components.graph_variant.eq(variant)].copy()
            edge_lineage = lineage[(lineage.tolerance_px == tolerance) & lineage.graph_variant.eq(variant) & lineage.object_type.eq("EDGE")]
            corridor_lineage = lineage[(lineage.tolerance_px == tolerance) & lineage.graph_variant.eq(variant) & lineage.object_type.eq("CORRIDOR")]
            represented = set(edges.source_edge_id.astype(str))
            if represented != source_ids:
                raise RuntimeError(f"C{year}:{tolerance}:{variant}:SOURCE_COVERAGE_FAILED")
            if set(edges.topo_edge_id.astype(str)) != set(geo_edges.topo_edge_id.astype(str)):
                raise RuntimeError(f"C{year}:{tolerance}:{variant}:MAP_GEO_EDGE_IDS_FAILED")
            if set(mode_nodes.node_id.astype(str)) != set(mode_geo_nodes.node_id.astype(str)):
                raise RuntimeError(f"C{year}:{tolerance}:{variant}:MAP_GEO_NODE_IDS_FAILED")
            if set(mode_corridors.corridor_id.astype(str)) != set(mode_geo_corridors.corridor_id.astype(str)):
                raise RuntimeError(f"C{year}:{tolerance}:{variant}:MAP_GEO_CORRIDOR_IDS_FAILED")
            if set(mode_components.component_id.astype(str)) != set(mode_geo_components.component_id.astype(str)):
                raise RuntimeError(f"C{year}:{tolerance}:{variant}:MAP_GEO_COMPONENT_IDS_FAILED")
            if set(edges.topo_edge_id.astype(str)) != set(edge_lineage.object_id.astype(str)) or len(edge_lineage) != len(edges):
                raise RuntimeError(f"C{year}:{tolerance}:{variant}:EDGE_LINEAGE_FAILED")
            if set(mode_corridors.corridor_id.astype(str)) != set(corridor_lineage.object_id.astype(str)):
                raise RuntimeError(f"C{year}:{tolerance}:{variant}:CORRIDOR_LINEAGE_FAILED")
            if not set(edges.from_node.astype(str)).issubset(set(mode_nodes.node_id.astype(str))) or not set(edges.to_node.astype(str)).issubset(set(mode_nodes.node_id.astype(str))):
                raise RuntimeError(f"C{year}:{tolerance}:{variant}:NODE_REFERENCES_FAILED")
            if int(edges.new_connector_created.sum()) != 0:
                raise RuntimeError(f"C{year}:{tolerance}:{variant}:NEW_CONNECTOR_FOUND")
            length = float(edges.geometry.length.sum())
            if abs(length - source_length) > max(1e-6, source_length * 1e-10):
                raise RuntimeError(f"C{year}:{tolerance}:{variant}:LENGTH_NOT_PRESERVED")
            source_roles = source_map.set_index("edge_id").source_role.astype(str).to_dict()
            if any(str(row.source_role) != source_roles[str(row.source_edge_id)] for row in edges.itertuples()):
                raise RuntimeError(f"C{year}:{tolerance}:{variant}:SOURCE_ROLE_CHANGED")
            edge_lookup = edges.set_index("topo_edge_id")
            degree_lookup = mode_nodes.set_index("node_id").degree.astype(int).to_dict()
            edge_lineage_lookup = edge_lineage.set_index("topo_edge_id")
            corridor_lineage_groups = {
                str(object_id): group for object_id, group in corridor_lineage.groupby("object_id", sort=False)
            }
            for edge in edges.itertuples():
                row = edge_lineage_lookup.loc[str(edge.topo_edge_id)]
                if (
                    str(row.source_edge_id) != str(edge.source_edge_id)
                    or abs(float(row.source_fraction_start) - float(edge.source_fraction_start)) > 1e-12
                    or abs(float(row.source_fraction_end) - float(edge.source_fraction_end)) > 1e-12
                ):
                    raise RuntimeError(f"C{year}:{tolerance}:{variant}:EDGE_LINEAGE_FRACTION_FAILED")
            for corridor in mode_corridors.itertuples():
                edge_ids = [value for value in str(corridor.topo_edge_ids).split(";") if value]
                if len(edge_ids) != int(corridor.topo_edge_count):
                    raise RuntimeError(f"C{year}:{tolerance}:{variant}:CORRIDOR_EDGE_LIST_FAILED")
                selected = edge_lookup.loc[edge_ids]
                occurrences: dict[str, int] = {}
                for edge in selected.itertuples():
                    occurrences[str(edge.from_node)] = occurrences.get(str(edge.from_node), 0) + 1
                    occurrences[str(edge.to_node)] = occurrences.get(str(edge.to_node), 0) + 1
                non_degree_two = [node for node in occurrences if degree_lookup[node] != 2]
                if len(non_degree_two) > 2:
                    raise RuntimeError(f"C{year}:{tolerance}:{variant}:CORRIDOR_CROSSES_BRANCH")
                corridor_rows = corridor_lineage_groups.get(str(corridor.corridor_id), corridor_lineage.iloc[0:0])
                if len(corridor_rows) != len(edge_ids) or set(corridor_rows.topo_edge_id.astype(str)) != set(edge_ids):
                    raise RuntimeError(f"C{year}:{tolerance}:{variant}:CORRIDOR_MEMBER_LINEAGE_FAILED")
                for row in corridor_rows.itertuples():
                    edge = edge_lookup.loc[str(row.topo_edge_id)]
                    if (
                        str(row.source_edge_id) != str(edge.source_edge_id)
                        or abs(float(row.source_fraction_start) - float(edge.source_fraction_start)) > 1e-12
                        or abs(float(row.source_fraction_end) - float(edge.source_fraction_end)) > 1e-12
                    ):
                        raise RuntimeError(f"C{year}:{tolerance}:{variant}:CORRIDOR_FRACTION_LINEAGE_FAILED")
                if abs(float(selected.pixel_length.sum()) - float(corridor.pixel_length)) > 1e-7:
                    raise RuntimeError(f"C{year}:{tolerance}:{variant}:CORRIDOR_LENGTH_FAILED")
            component_lookup = mode_components.set_index("component_id")
            if set(edges.component_id.astype(str)) != set(component_lookup.index.astype(str)):
                raise RuntimeError(f"C{year}:{tolerance}:{variant}:COMPONENT_COVERAGE_FAILED")
            for component_id, selected in edges.groupby("component_id"):
                component = component_lookup.loc[str(component_id)]
                if int(component.edge_count) != len(selected) or abs(float(component.pixel_length) - float(selected.pixel_length.sum())) > 1e-7:
                    raise RuntimeError(f"C{year}:{tolerance}:{variant}:COMPONENT_AGGREGATE_FAILED")
            mode_checks.append({
                "tolerance_px": tolerance, "graph_variant": variant, "edges": len(edges), "nodes": len(mode_nodes),
                "corridors": len(mode_corridors), "length_px": length, "source_edges_represented": len(represented),
                "new_connectors": 0, "pass": True,
            })
    metric_layers = (
        "routes_topologiques_strictes", "routes_topologiques_larges",
        "routes_topologiques_strictes_geographiques", "routes_topologiques_larges_geographiques",
        "corridors_continus", "corridors_continus_geographiques",
        "composantes_connexes", "composantes_connexes_geographiques",
    )
    if any("km" in column.lower() for layer in metric_layers for column in gpd.read_file(product, layer=layer, rows=1).columns):
        raise RuntimeError(f"C{year}:KILOMETRIC_FIELD_PREMATURE")
    qgis_projects = [
        directory / f"C{year}_RESEAU_TOPOLOGIQUE_CARTE.qgs",
        directory / f"C{year}_RESEAU_TOPOLOGIQUE_GEOGRAPHIQUE.qgs",
    ]
    for project in qgis_projects:
        if not project.exists():
            raise RuntimeError(f"C{year}:QGIS_PROJECT_MISSING:{project.name}")
        ET.parse(project)
        project_text = project.read_text(encoding="utf-8")
        if str(product.as_posix()) not in project_text:
            raise RuntimeError(f"C{year}:QGIS_PROJECT_WRONG_DATASOURCE:{project.name}")
    validation = source_map_validation(year)
    full_map_validated = bool(validation.get("full_map_validated_by_farouk"))
    result = {
        "status": "PASS_TECHNICAL_TOPOLOGY_AND_LINEAGE",
        "scientific_status": "TECHNICALLY_VERIFIED_TOPOLOGY_DERIVED_FROM_FAROUK_VALIDATED_MAP" if full_map_validated else "VECTOR_DERIVED_TOPOLOGY_NOT_HUMAN_VALIDATED_AND_NOT_HISTORICAL_CONNECTIVITY_PROOF",
        "source_map_scientific_validation": validation,
        "map_year": year,
        "source_sha256": sha256(source),
        "output_sha256": sha256(product),
        "sqlite_integrity": integrity,
        "required_layers": sorted(REQUIRED),
        "required_sql_views": sorted(REQUIRED_VIEWS),
        "qgis_projects": [str(path) for path in qgis_projects],
        "input_edges": len(source_map),
        "input_fragments": int(source_map.source_role.eq("V1_FRAGMENT").sum()),
        "input_existing_connectors": int(source_map.source_role.eq("PI_RULE_CONNECTOR").sum()),
        "modes": mode_checks,
    }
    verification = directory / f"C{year}_RESEAU_TOPOLOGIQUE.verification.json"
    verification.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, choices=sorted(PRODUCTS))
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    years = sorted(PRODUCTS) if args.all else [args.year]
    if not args.all and args.year is None:
        parser.error("--year ou --all requis")
    results = [verify_year(year) for year in years]
    print(json.dumps({"status": "PASS", "years": [result["map_year"] for result in results]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
