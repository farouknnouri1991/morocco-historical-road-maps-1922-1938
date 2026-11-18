#!/usr/bin/env python3
"""Vérifier le produit 1925 extrait du JPEG et le comparer au meilleur réseau antérieur."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import box
from shapely.ops import unary_union


EXPECTED_TABLES = {
    "routes_1925_carte", "localites_1925_carte", "villes_article_1925_carte",
    "routes_1925_geographiques", "localites_1925_geographiques", "villes_article_1925_geographiques",
    "routes_maroc_actuel", "localites_maroc_actuel", "carte_originale_1925", "carte_1925_georeferencee",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().lower()


def coverage(left, right, tolerance: float) -> float:
    left_union = unary_union(left)
    right_union = unary_union(right)
    return float(left_union.intersection(right_union.buffer(tolerance)).length / left_union.length) if left_union.length else 0.0


def fail(message: str) -> None:
    raise RuntimeError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config_path = args.config.resolve(strict=True)
    root = config_path.parent.parent
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output = (root / config["output_geopackage"]).resolve(strict=True)
    reference = (root / config["reference_geopackage"]).resolve(strict=True)
    height = int(config["height"])
    x0, y0, x1, y1 = config["documentary_exclusion_boxes_source_down"][0]
    documentary_mask = box(x0, height - y1, x1, height - y0)

    reference_observed = gpd.read_file(reference, layer="roads_observed_current_best")
    reference_connectors = gpd.read_file(reference, layer="road_connectors_current_best")
    reference_localities = gpd.read_file(reference, layer="locality_candidates")
    reference_observed = reference_observed[~reference_observed.geometry.intersects(documentary_mask)].copy()
    reference_connectors = reference_connectors[~reference_connectors.geometry.intersects(documentary_mask)].copy()
    reference_localities = reference_localities[reference_localities.disposition.eq("KEEP_AUTOMATED_CANDIDATE") & ~reference_localities.geometry.intersects(documentary_mask)].copy()

    roads_map = gpd.read_file(output, layer="routes_1925_carte")
    localities_map = gpd.read_file(output, layer="localites_1925_carte")
    cities_map = gpd.read_file(output, layer="villes_article_1925_carte")
    roads_geo = gpd.read_file(output, layer="routes_1925_geographiques")
    localities_geo = gpd.read_file(output, layer="localites_1925_geographiques")
    cities_geo = gpd.read_file(output, layer="villes_article_1925_geographiques")
    current_roads = gpd.read_file(output, layer="routes_maroc_actuel")
    current_cities = gpd.read_file(output, layer="localites_maroc_actuel")
    candidate_observed = roads_map[roads_map.source_role.eq("V1_FRAGMENT")]
    candidate_connectors = roads_map[roads_map.source_role.eq("PI_RULE_CONNECTOR")]

    counts = {
        "road_fragments": len(candidate_observed), "road_connectors": len(candidate_connectors), "roads": len(roads_map),
        "historical_localities": len(localities_map), "human_validated_localities": int(localities_map.validation_humaine.sum()),
        "article_cities": len(cities_map), "modern_roads": len(current_roads), "modern_localities": len(current_cities), "modern_article_cities": int(current_cities.ville_article_core28.sum()),
    }
    if len(roads_map) != len(roads_geo) or len(localities_map) != len(localities_geo) or len(cities_map) != len(cities_geo):
        fail("COMPTES_CARTE_GEOGRAPHIE_NON_ALIGNES")
    if counts["human_validated_localities"] != 28 or counts["article_cities"] != 28 or counts["modern_article_cities"] != 28:
        fail(f"CONTROLES_HUMAINS_28_INCOMPLETS:{counts}")
    if counts["modern_localities"] != counts["historical_localities"] or set(current_cities.locality_id.astype(str)) != set(localities_map.locality_id.astype(str)):
        fail(f"CATALOGUE_ACTUEL_INCOMPLET_PAR_RAPPORT_AUX_LOCALITES_HISTORIQUES:{counts}")
    automatic_localities = localities_map[localities_map.validation_humaine.eq(0)]
    if not automatic_localities.effective_road_relation.fillna(False).all():
        fail("LOCALITE_AUTOMATIQUE_SANS_RELATION_ROUTIERE_EFFECTIVE")

    decisions = pd.read_csv((root / config["human_locality_decisions"]).resolve(strict=True))
    expected_points = {(round(float(x), 6), round(height - float(y), 6)) for x, y in zip(decisions.pi_clicked_x_source_px, decisions.pi_clicked_y_source_down_px)}
    delivered_points = {(round(point.x, 6), round(point.y, 6)) for point in cities_map.geometry}
    if expected_points != delivered_points:
        fail("CLICS_HUMAINS_NON_PRESERVES_SUR_CARTE")
    if cities_geo.geometry.equals(current_cities.geometry):
        fail("CERCLES_HISTORIQUES_CONFONDUS_AVEC_POSITIONS_ACTUELLES")

    road_comparison = {
        "reference_fragments": len(reference_observed), "candidate_fragments": len(candidate_observed),
        "reference_connectors": len(reference_connectors), "candidate_connectors": len(candidate_connectors),
        "reference_observed_length_px": round(float(unary_union(reference_observed.geometry).length), 3),
        "candidate_observed_length_px": round(float(unary_union(candidate_observed.geometry).length), 3),
        "reference_covered_by_candidate_at_3px": round(coverage(reference_observed.geometry, candidate_observed.geometry, 3), 6),
        "reference_covered_by_candidate_at_5px": round(coverage(reference_observed.geometry, candidate_observed.geometry, 5), 6),
        "candidate_confirmed_by_reference_at_3px": round(coverage(candidate_observed.geometry, reference_observed.geometry, 3), 6),
        "candidate_confirmed_by_reference_at_5px": round(coverage(candidate_observed.geometry, reference_observed.geometry, 5), 6),
        "connector_reference_covered_at_5px": round(coverage(reference_connectors.geometry, candidate_connectors.geometry, 5), 6),
        "connector_candidate_confirmed_at_5px": round(coverage(candidate_connectors.geometry, reference_connectors.geometry, 5), 6),
        "complete_reference_network_covered_at_5px": round(coverage(pd.concat([reference_observed.geometry, reference_connectors.geometry]), roads_map.geometry, 5), 6),
        "complete_candidate_network_confirmed_at_5px": round(coverage(roads_map.geometry, pd.concat([reference_observed.geometry, reference_connectors.geometry]), 5), 6),
    }
    if road_comparison["reference_covered_by_candidate_at_5px"] < 0.98:
        fail(f"COUVERTURE_RESEAU_ANTERIEUR_INSUFFISANTE:{road_comparison}")
    if road_comparison["candidate_confirmed_by_reference_at_3px"] < 0.95:
        fail(f"AJOUTS_ROUTIERS_TROP_ELOIGNES_DE_LA_REFERENCE:{road_comparison}")

    reference_locality_union = unary_union(reference_localities.geometry)
    candidate_locality_union = unary_union(localities_map.geometry)
    locality_comparison = {
        "reference_automatic_localities": len(reference_localities),
        "candidate_all_localities_including_28_human": len(localities_map),
        "reference_with_candidate_within_15px": int(sum(point.distance(candidate_locality_union) <= 15 for point in reference_localities.geometry)),
        "candidate_with_reference_within_15px": int(sum(point.distance(reference_locality_union) <= 15 for point in localities_map.geometry)),
    }

    connection = sqlite3.connect(output)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT table_name FROM gpkg_contents")}
        rasters = {row[0]: list(row) for row in connection.execute("SELECT table_name,min_x,min_y,max_x,max_y,srs_id FROM gpkg_contents WHERE data_type='tiles'")}
    finally:
        connection.close()
    if integrity != "ok":
        fail(f"INTEGRITE_SQLITE:{integrity}")
    if tables != EXPECTED_TABLES:
        fail(f"TABLES_INATTENDUES:{sorted(tables)}")
    if set(rasters) != {"carte_originale_1925", "carte_1925_georeferencee"} or rasters["carte_originale_1925"][-1] != -1 or rasters["carte_1925_georeferencee"][-1] != 4326:
        fail(f"RASTERS_INCORRECTS:{rasters}")

    result = {
        "status": "PASS_C1925_SOURCE_FIRST_TECHNICAL_AND_REFERENCE_COMPARISON",
        "epistemic_status": "CURRENT_SOURCE_FIRST_CANDIDATE_NOT_HUMAN_VALIDATED_EXHAUSTIVELY",
        "output_sha256": sha256(output), "counts": counts, "road_comparison": road_comparison,
        "locality_comparison": locality_comparison, "human_clicks_preserved_exactly": True,
        "sqlite_integrity": integrity, "rasters": rasters,
        "scientific_limit": "THE_PRIOR_AUTOMATIC_PACKAGE_IS_A_COMPARATOR_NOT_GROUND_TRUTH; AUTOMATIC_LOCALITIES_CAN_INCLUDE_FALSE_POSITIVES_AND_FALSE_NEGATIVES",
    }
    report = output.with_suffix(".verification.json")
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
