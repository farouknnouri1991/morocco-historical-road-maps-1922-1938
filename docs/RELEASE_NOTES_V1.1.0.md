# Version 1.1.0 — Original scans and open source-to-QGIS audit trail

Version 1.1.0 adds the primary cartographic evidence and the controls needed to inspect how each public vector layer relates to its historical source image. It does not replace or alter the twelve geographic road and locality layers published in version 1.0.0.

## Added

- Six immutable original JPEG reproductions for 1922, 1925, 1928, 1934, 1936 and 1938, verified against the private controlling copies by SHA-256.
- Exact BnF catalogue notices, Gallica digital-object links, shelf marks, displayed dates, pixel dimensions and required attribution for every scan.
- One-pixel QGIS world files for the original scans and a tiled 1938 display derivative required by the JPEG decoder's memory limit.
- Six minimal GeoPackages containing the road and locality vectors in source-image coordinates.
- Eighteen source-control layers in the master QGIS project, alongside the existing twelve geographic layers.
- Reproducible build and verification scripts for the source package and the thirty-layer QGIS project.

## How to audit a map

Open `qgis/MOROCCO_HISTORICAL_MAPS_1922_1938.qgz`, enable **Source images — control in pixel coordinates**, and then enable one annual subgroup. The cyan road vectors and yellow locality points are drawn directly over the historical image. The ordinary annual groups retain the WGS 84 layers used for geographic analysis and cross-year comparison.

## Rights

The six scans and the 1938 raster display derivative are outside the repository's CC BY-NC 4.0 licence. They are supplied for non-commercial scientific reuse under the Gallica content terms with the attribution **“Source gallica.bnf.fr / Bibliothèque nationale de France”** and an item-specific Gallica link. The derived vectors, documentation and configurations retain the repository licences stated at the root.

## Verification

The release candidate passed the following controls: all six public JPEGs are byte-identical to their controlling copies; all six institutional records and digital objects are linked; all thirty QGIS layers are valid; all project paths are relative; all six source-image overlays render with the vectors aligned to their rasters; and no ordinary repository file reaches 50 MiB.
