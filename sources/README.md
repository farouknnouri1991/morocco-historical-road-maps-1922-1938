# Original scans and institutional provenance

This directory gives direct access to the six immutable source reproductions used to derive the public road and locality layers. `ORIGINAL_SCAN_MANIFEST.csv` identifies each file by SHA-256 digest, pixel dimensions, BnF catalogue notice, Gallica digital object, shelf mark, scale, and required attribution. `ORIGINAL_SCAN_TECHNICAL_MANIFEST.json` provides the same file-level checksums in a machine-readable form and links each scan to its source-image control GeoPackage.

The JPEG files are faithful source reproductions. They are not geometrically edited, recoloured, cropped, or overwritten by the publication workflow. The publication script copies them without transformation and verifies their SHA-256 digests.

## From the original image to QGIS

For each year, `../data/source_image_control/CYYYY_SOURCE_IMAGE_VECTORS.gpkg` contains the extracted road lines and localities in source-image pixel space. A six-line `.jgw` world file gives the original JPEG a one-pixel coordinate grid in QGIS. The master project `../qgis/MOROCCO_HISTORICAL_MAPS_1922_1938.qgz` opens all six controls under **Source images — control in pixel coordinates** and also retains the twelve geographic layers used for cross-year comparison. Because the 1938 JPEG exceeds the normal memory limit of the QGIS JPEG decoder, the project displays `C1938_QGIS_DISPLAY.tif`, a tiled derivative with the same pixel grid; the immutable JPEG remains the checksum reference and documentary source.

The chain is therefore inspectable in two complementary spaces:

1. Original JPEG: immutable documentary source and checksum reference.
2. Original raster opened in QGIS with its one-pixel world file, or the aligned tiled display derivative for 1938.
3. Routes and localities in source-image pixels: visual control of what was extracted from the map.
4. Routes and localities in WGS 84 geographic coordinates: comparison across years and connection to other spatial data.

Pixel-space and geographic objects retain their year and source identifiers. The geographic transformation does not change the historical content represented; it changes the coordinate space in which the derived geometry is expressed.

## Reuse conditions for the scans

The scans are Gallica reproductions from the collections of the Bibliothèque nationale de France. Gallica permits free non-commercial reuse provided that the source is maintained as **“Source gallica.bnf.fr / Bibliothèque nationale de France”**. Commercial reuse is subject to the conditions and licensing process stated by Gallica. The six JPEG files and the 1938 tiled display derivative are consequently excluded from the repository's CC BY-NC 4.0 licence; their controlling terms are the [Gallica conditions of use](https://gallica.bnf.fr/accueil/fr/html/conditions-dutilisation-de-gallica).

The derived vector layers, documentation, and public configuration files remain governed by the repository licences stated at the root.
