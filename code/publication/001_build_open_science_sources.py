"""Build the public source-image package without modifying private originals.

The script copies the six immutable JPEG reproductions, writes a one-pixel
world file for direct QGIS display, and creates one slim GeoPackage per year
containing the routes and localities expressed in source-image pixel
coordinates. Geographic layers remain in the public cross-year GeoPackage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path


YEARS = (1922, 1925, 1928, 1934, 1936, 1938)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def locate_consolidated(project_root: Path, year: int) -> Path:
    candidates = sorted(
        (project_root / f"REPRODUCTION_{year}" / "outputs").glob(
            "*PRODUIT*CONSOLIDE*.gpkg"
        )
    )
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one consolidated GeoPackage for {year}, found {len(candidates)}"
        )
    return candidates[0]


def slim_source_geopackage(source: Path, target: Path, year: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    allowed = {
        f"routes_{year}_carte",
        f"localites_{year}_carte",
    }

    connection = sqlite3.connect(target)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        objects = connection.execute(
            "SELECT type, name, tbl_name FROM sqlite_master "
            "WHERE type IN ('table', 'view') ORDER BY name"
        ).fetchall()
        keep_prefixes = tuple(f"rtree_{name}_" for name in allowed)
        for object_type, name, table_name in objects:
            if name.startswith(("gpkg_", "sqlite_")):
                continue
            if name in allowed or name.startswith(keep_prefixes):
                continue
            quoted = name.replace('"', '""')
            connection.execute(f'DROP {object_type.upper()} IF EXISTS "{quoted}"')

        placeholders = ",".join("?" for _ in allowed)
        for metadata_table in (
            "gpkg_contents",
            "gpkg_geometry_columns",
            "gpkg_ogr_contents",
            "gpkg_tile_matrix",
            "gpkg_tile_matrix_set",
        ):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (metadata_table,),
            ).fetchone()
            if exists:
                connection.execute(
                    f"DELETE FROM {metadata_table} WHERE table_name NOT IN ({placeholders})",
                    tuple(allowed),
                )

        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpkg_extensions'"
        ).fetchone():
            connection.execute(
                f"DELETE FROM gpkg_extensions WHERE table_name IS NOT NULL "
                f"AND table_name NOT IN ({placeholders})",
                tuple(allowed),
            )
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpkg_metadata_reference'"
        ).fetchone():
            connection.execute(
                f"DELETE FROM gpkg_metadata_reference WHERE table_name IS NOT NULL "
                f"AND table_name NOT IN ({placeholders})",
                tuple(allowed),
            )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    repo_root = args.repo_root.resolve()
    scan_dir = repo_root / "sources" / "original_scans"
    control_dir = repo_root / "data" / "source_image_control"
    scan_dir.mkdir(parents=True, exist_ok=True)
    control_dir.mkdir(parents=True, exist_ok=True)

    records = []
    for year in YEARS:
        source_scan = (
            project_root
            / f"REPRODUCTION_{year}"
            / "inputs"
            / "carte_originale"
            / f"C{year}_CARTE_ORIGINALE.jpg"
        )
        if not source_scan.is_file():
            raise FileNotFoundError(source_scan)
        public_scan = scan_dir / source_scan.name
        shutil.copy2(source_scan, public_scan)

        source_gpkg = locate_consolidated(project_root, year)
        connection = sqlite3.connect(source_gpkg)
        try:
            raster_height = connection.execute(
                "SELECT max_y FROM gpkg_tile_matrix_set WHERE table_name=?",
                (f"carte_originale_{year}",),
            ).fetchone()[0]
        finally:
            connection.close()
        world_file = public_scan.with_suffix(".jgw")
        world_file.write_text(
            f"1\n0\n0\n-1\n0.5\n{raster_height - 0.5:g}\n", encoding="ascii"
        )

        public_gpkg = control_dir / f"C{year}_SOURCE_IMAGE_VECTORS.gpkg"
        slim_source_geopackage(source_gpkg, public_gpkg, year)
        records.append(
            {
                "year": year,
                "scan": public_scan.relative_to(repo_root).as_posix(),
                "scan_bytes": public_scan.stat().st_size,
                "scan_sha256": sha256(public_scan),
                "world_file": world_file.relative_to(repo_root).as_posix(),
                "world_file_sha256": sha256(world_file),
                "source_image_geopackage": public_gpkg.relative_to(repo_root).as_posix(),
                "source_image_geopackage_bytes": public_gpkg.stat().st_size,
                "source_image_geopackage_sha256": sha256(public_gpkg),
                "source_layers": [
                    f"routes_{year}_carte",
                    f"localites_{year}_carte",
                ],
            }
        )

    manifest = repo_root / "sources" / "ORIGINAL_SCAN_TECHNICAL_MANIFEST.json"
    manifest.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
