from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
YEARS = (1922, 1925, 1928, 1934, 1936, 1938)
PRODUCT_NAMES = {
    1922: "C1922_PRODUIT_FINAL_CONSOLIDE.gpkg",
    1925: "C1925_PRODUIT_CONSOLIDE_ESSAI.gpkg",
    1928: "C1928_PRODUIT_FINAL_CONSOLIDE.gpkg",
    1934: "C1934_PRODUIT_FINAL_CONSOLIDE.gpkg",
    1936: "C1936_PRODUIT_FINAL_CONSOLIDE.gpkg",
    1938: "C1938_PRODUIT_FINAL_CONSOLIDE.gpkg",
}
PRODUCER_FAMILY = {
    1922: "generateur_annuel_anterieur",
    1925: "generateur_annuel_anterieur",
    1928: "generateur_annuel_anterieur",
    1934: "moteur_commun_1938_importe_directement",
    1936: "moteur_commun_1938_importe_directement",
    1938: "moteur_1938_controleur",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def audit_year(year: int) -> dict[str, object]:
    product = ROOT / f"REPRODUCTION_{year}" / "outputs" / PRODUCT_NAMES[year]
    if not product.exists():
        raise FileNotFoundError(product)
    historical_localities = f"localites_{year}_carte"
    geographic_localities = f"localites_{year}_geographiques"
    historical_roads = f"routes_{year}_carte"
    geographic_roads = f"routes_{year}_geographiques"
    with sqlite3.connect(product) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        contents = list(
            connection.execute(
                "SELECT table_name, data_type, srs_id FROM gpkg_contents ORDER BY table_name"
            )
        )
        counts = {
            table_name: connection.execute(
                f"SELECT COUNT(*) FROM {quote(table_name)}"
            ).fetchone()[0]
            for table_name, _, _ in contents
        }
        route_roles: dict[str, int] = {}
        if historical_roads in counts:
            columns = {
                row[1]
                for row in connection.execute(
                    f"PRAGMA table_info({quote(historical_roads)})"
                )
            }
            role_field = next(
                (
                    field
                    for field in ("source_role", "road_role", "role")
                    if field in columns
                ),
                None,
            )
            if role_field:
                route_roles = {
                    str(role): int(count)
                    for role, count in connection.execute(
                        f"SELECT {quote(role_field)}, COUNT(*) FROM {quote(historical_roads)} "
                        f"GROUP BY {quote(role_field)} ORDER BY {quote(role_field)}"
                    )
                }
    historical_count = counts.get(historical_localities, 0)
    geographic_count = counts.get(geographic_localities, 0)
    modern_count = counts.get("localites_maroc_actuel", 0)
    if year == 1938:
        modern_relation = "catalogue_avant_masque_587_vs_historique_apres_masque_585"
        modern_relation_ok = historical_count == 585 and modern_count == 587
    else:
        modern_relation = "une_position_actuelle_par_localite_historique_retenue"
        modern_relation_ok = historical_count == geographic_count == modern_count
    config_path = ROOT / f"REPRODUCTION_{year}" / "inputs" / f"configuration_{year}.json"
    scientific_validation: dict[str, object] = {}
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        scientific_validation = dict(config.get("scientific_validation", {}))
    return {
        "year": year,
        "product": str(product),
        "sha256": sha256(product),
        "sqlite_integrity": integrity,
        "producer_family": PRODUCER_FAMILY[year],
        "scientific_validation": scientific_validation,
        "tables": [table_name for table_name, _, _ in contents],
        "table_count": len(contents),
        "rasters": [table_name for table_name, data_type, _ in contents if data_type == "tiles"],
        "routes_historical": counts.get(historical_roads, 0),
        "routes_geographic": counts.get(geographic_roads, 0),
        "route_roles": route_roles,
        "localities_historical": historical_count,
        "localities_geographic": geographic_count,
        "localities_modern": modern_count,
        "modern_relation": modern_relation,
        "modern_relation_ok": modern_relation_ok,
        "modern_roads": counts.get("routes_maroc_actuel", 0),
        "counts_by_table": counts,
        "technical_pass": (
            integrity == "ok"
            and counts.get(historical_roads, 0) == counts.get(geographic_roads, 0)
            and historical_count == geographic_count
            and modern_relation_ok
            and len([table for table, data_type, _ in contents if data_type == "tiles"]) == 2
        ),
    }


def main() -> None:
    rows = [audit_year(year) for year in YEARS]
    timestamp = datetime.now(ZoneInfo("Africa/Casablanca")).isoformat(timespec="seconds")
    report = {
        "generated_at": timestamp,
        "scope": "six_millesimes_active_products",
        "scientific_boundary": (
            "A technical pass establishes package integrity and internal count consistency. "
            "Scientific validations are recorded separately and remain controlling; the complete 1922 map "
            "is explicitly validated by Farouk."
        ),
        "common_engine_contract": (
            "1934 and 1936 import the 1938 engine directly. The 1922, 1925 and 1928 "
            "outputs remain reproducible annual implementations and are not falsely relabelled "
            "as direct executions of the shared engine."
        ),
        "years": rows,
        "all_technical_pass": all(bool(row["technical_pass"]) for row in rows),
    }
    documentation = ROOT / "documentation"
    json_path = documentation / "CONTROLE_TRANSVERSAL_1922_1938.json"
    csv_path = documentation / "TABLEAU_RESULTATS_1922_1938.csv"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fieldnames = [
        "year",
        "producer_family",
        "routes_historical",
        "localities_historical",
        "localities_modern",
        "table_count",
        "sqlite_integrity",
        "modern_relation_ok",
        "technical_pass",
        "sha256",
        "product",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in fieldnames})
    print(json.dumps({"all_technical_pass": report["all_technical_pass"], "rows": rows}, ensure_ascii=False))


if __name__ == "__main__":
    main()
