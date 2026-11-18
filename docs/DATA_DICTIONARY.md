# Data dictionary

The file `data/morocco_historical_maps_1922_1938.gpkg` contains twelve WGS84 geographic layers. Each annual pair consists of `routes_YEAR` and `localities_YEAR`.

## Common publication fields

| Field | Meaning |
|---|---|
| `publication_year` | Historical map year represented by the layer. |
| `publication_family` | `routes` or `localities`. |
| `scientific_status` | Year-level publication status added without replacing the object-level source status. |

## Route layers

| Field family | Meaning |
|---|---|
| identifiers | Stable edge, source-feature and node identifiers carried from the controlling annual product. |
| `source_role`, `origine_objet` | Whether the geometry represents an observed fragment or an admitted connector. |
| `network_class`, `operational_status` | Interpretation supported by the map conventions. |
| `pixel_length` | Length in the original map coordinate system; it is not kilometres. |
| `admissibility_status`, `source_method` | Object-level construction and admissibility provenance. |
| `georeference_model`, `georeference_status` | Transformation used to place the object in geographic coordinates and its status. |

## Locality layers

| Field family | Meaning |
|---|---|
| `circle_candidate_id`, `label_candidate_id`, `locality_id` | Identifiers of the map symbol, label and retained locality object. |
| `historical_ocr_text` | Text read from the historical map; transcription errors can remain. |
| `gazetteer_*`, `geonameid` | Contemporary gazetteer candidate used for naming or positioning. |
| `map_pixel_x`, `map_pixel_y` | Location in the original map coordinate system. |
| `observed_map_lon`, `observed_map_lat` | Geographic location obtained from the map transformation. |
| distance and similarity fields | Diagnostics used in the automatic association process; they are not uncertainty intervals. |
| `road_relation_type`, `effective_road_relation` | How the candidate locality relates to the mapped road network. |
| `automatic_tier`, `status`, `validation_humaine` | Object-level status distinguishing automatic candidates from human-controlled objects. |

The exact schema varies slightly by year because the annual maps and retained evidence are not identical. Use `data/LAYER_SUMMARY.csv` for counts and controlling-source lineage.
