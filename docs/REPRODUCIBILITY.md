# Reproducibility guide

## Public package

The public repository is immediately usable for geographic analysis through the consolidated GeoPackage and master QGIS project. No private folder or embedded runtime is required to open those products.

## Rebuilding from historical source scans

Full reconstruction requires the historical scans, the GeoNames Morocco extract and, for contemporary comparison modules, an OpenStreetMap Morocco extract. These large external inputs are not redistributed here. Place independently obtained files at the paths declared in the annual JSON configurations, then create a Python environment from `requirements.txt`.

The annual scripts are stored under `code/annual/YEAR`. The settings and small controlled inputs are under `config/annual/YEAR`. The routable-network modules and their configurations are under `code/routable` and `config/routable`.

## Public data build

The consolidated public GeoPackage is a faithful extraction of the geographic road and locality layers from the six controlling annual GeoPackages. `data/LAYER_SUMMARY.csv` records the source file, source layer, source SHA-256 hash, public layer and feature count. `PUBLICATION_MANIFEST.json` records the hashes and sizes of all public files.

Technical equality of feature counts and geometry hashes establishes faithful packaging. It does not create a new scientific validation of automatically classified objects.
