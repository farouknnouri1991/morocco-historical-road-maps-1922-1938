#!/usr/bin/env python3
"""Transférer les 28 contrôles géographiques d'une carte de référence vers un nouveau cadre cartographique."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config_path = args.config.resolve(strict=True)
    root = config_path.parent.parent
    config = json.loads(config_path.read_text(encoding="utf-8"))
    transfer = config["automatic_georeference_transfer"]
    source = (root / transfer["source_controls"]).resolve(strict=True)
    target = (root / config["automatic_georeference_controls"]).resolve()
    source_frame = [float(value) for value in transfer["source_frame"]]
    target_frame = [float(value) for value in config["map_frame"]]
    controls = pd.read_csv(source)
    x0s, y0s, x1s, y1s = source_frame
    x0t, y0t, x1t, y1t = target_frame
    controls["map_pixel_x_candidate"] = x0t + (controls.map_pixel_x_candidate - x0s) * (x1t - x0t) / (x1s - x0s)
    controls["map_pixel_y_candidate"] = y0t + (controls.map_pixel_y_candidate - y0s) * (y1t - y0t) / (y1s - y0s)
    controls["control_status"] = f"AUTOMATIC_FRAME_TRANSFER_FROM_{transfer['source_map']}_CONTROLS_NOT_HUMAN_VALIDATED_FOR_{config['map_id']}"
    target.parent.mkdir(parents=True, exist_ok=True)
    controls.to_csv(target, index=False)
    report = {
        "status": "AUTOMATIC_GEOREFERENCE_CONTROLS_PREPARED",
        "target_map": config["map_id"],
        "source_map": transfer["source_map"],
        "source_controls": str(source),
        "target_controls": str(target),
        "control_count": len(controls),
        "source_frame": source_frame,
        "target_frame": target_frame,
        "scientific_status": "AUTOMATIC_INHERITED_GEOREFERENCE_NOT_HUMAN_VALIDATED_FOR_TARGET_YEAR",
    }
    target.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
