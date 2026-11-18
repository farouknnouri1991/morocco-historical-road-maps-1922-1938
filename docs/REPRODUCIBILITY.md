# Reproducibility guide

## Public package

The public repository is immediately usable for geographic analysis through the consolidated GeoPackage and master QGIS project. No private folder or embedded runtime is required to open those products.

## Rebuilding from historical source scans

The six historical scans used by the annual pipelines are included under `sources/original_scans/` and identified by checksums and institutional links in `sources/ORIGINAL_SCAN_MANIFEST.csv`. Full reconstruction also requires the GeoNames Morocco extract and, for contemporary comparison modules, an OpenStreetMap Morocco extract. Those external databases are not redistributed here. Place independently obtained files at the paths declared in the annual JSON configurations, then create a Python environment from `requirements.txt`.

The annual scripts are stored under `code/annual/YEAR`. The settings and small controlled inputs are under `config/annual/YEAR`. The routable-network modules and their configurations are under `code/routable` and `config/routable`.

## Auditing the image-to-geography transformation

The master QGIS project contains two complementary views. The source-image groups draw the retained roads and localities directly over the original raster in pixel coordinates. The annual geographic groups draw the corresponding derived objects in WGS 84. The source-image GeoPackages contain only the two vector layers needed for this audit; `code/publication/001_build_open_science_sources.py` generates them and the one-pixel world files from the controlling annual GeoPackages. `code/publication/002_extend_master_qgis.py` builds the tiled 1938 display derivative and adds all source controls to the master project.

Run `code/publication/003_verify_open_science_package.py` with the QGIS Python environment to check the six scan digests, the contents of all source-image GeoPackages, the validity of every project layer and the use of relative paths.

## Public data build

The consolidated public GeoPackage is a faithful extraction of the geographic road and locality layers from the six controlling annual GeoPackages. `data/LAYER_SUMMARY.csv` records the source file, source layer, source SHA-256 hash, public layer and feature count. `PUBLICATION_MANIFEST.json` records the hashes and sizes of all public files.

Technical equality of feature counts and geometry hashes establishes faithful packaging. It does not create a new scientific validation of automatically classified objects.
