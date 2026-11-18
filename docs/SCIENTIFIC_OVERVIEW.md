# Scientific overview

## Research object

The project reconstructs the road network and mapped localities visible on six historical maps of Morocco between 1922 and 1938. The public data provide a common geographic representation for comparing mapped structures while retaining the year-specific interpretation and validation status of each object.

## Evidence carried by the public layers

| Year | Road objects | Locality objects | Public interpretation |
|---:|---:|---:|---|
| 1922 | 1,838 | 80 | The complete map, including its roads, was explicitly validated by the principal investigator. |
| 1925 | 1,628 | 270 | The 28 article cities are human-controlled; the remaining locality associations are automatic candidates. |
| 1928 | 10,420 | 165 | Roads and localities form a technically checked automatic reconstruction, not an object-by-object human validation. |
| 1934 | 14,463 | 224 | Automatic reconstruction using the shared 1938 engine with year-specific settings. |
| 1936 | 15,016 | 311 | Automatic reconstruction using the shared 1938 engine with year-specific settings. |
| 1938 | 13,170 | 585 | The product contains human-controlled article cities and service readings alongside automatic locality candidates. |

These counts reproduce the retained annual geographic layers. A road object is a vector fragment or an admitted gap connector, not necessarily a historically distinct road. A locality object is a mapped-circle and label association or a human-controlled location depending on its status fields.

## Construction logic

Historical evidence originates in the old map: red road traces, map symbols and printed labels. Contemporary gazetteers can help assign a current name or coordinate but do not create a historical object. Each annual product keeps a map-space representation for source inspection and a geographic representation for cross-year use. The public package distributes only the geographic road and locality layers.

The maps do not share identical graphic conventions, coverage or source quality. Cross-year comparisons must therefore distinguish historical change from changes in map design, detection performance and georeferencing. The public package does not claim that object counts alone measure road construction or urban growth.

## Scope of the public documentation

This overview records the interpretation, status and limits needed to use the retained layers. The six source scans are included with item-level BnF and Gallica links, file checksums and source-specific reuse terms. The master QGIS project also exposes the source-image vector layers so that extraction can be checked directly against each original raster.
