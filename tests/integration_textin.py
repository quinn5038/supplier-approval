"""Explicit live probe: send one generated, non-sensitive image to TextIn."""
import json
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import textin_pipeline as tp


def main():
    with tempfile.TemporaryDirectory(prefix="supplier-textin-probe-") as location:
        tp.BASE = Path(location)
        tp.FILES_DESENS_DIR = tp.BASE / "files_cache_desens"
        file = tp.FILES_DESENS_DIR / "probe" / "synthetic.png"
        file.parent.mkdir(parents=True)
        image = np.full((160, 650, 3), 255, np.uint8)
        cv2.putText(image, "TEST 12345", (25, 95), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 2)
        cv2.imwrite(str(file), image)
        (tp.BASE / "cache_v4.json").write_text(json.dumps({"probe": {"materials_detail": [
            {"fileName": file.name, "types": ["business_license"]}]}}), encoding="utf-8")
        markdown, _ = tp.parse_file_textin(file, "business_license")
        if not markdown:
            raise AssertionError("TextIn returned no text for generated image")
        print("PASS: TextIn accepted configured credentials and returned text for a synthetic image")


if __name__ == "__main__":
    main()
