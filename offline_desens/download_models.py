"""Explicit setup step: download official weights, never supplier documents."""
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

MODELS = {
    "det": "https://paddleocr.bj.bcebos.com/PP-OCRv4/chinese/ch_PP-OCRv4_det_infer.tar",
    "rec": "https://paddleocr.bj.bcebos.com/PP-OCRv4/chinese/ch_PP-OCRv4_rec_infer.tar",
    "cls": "https://paddleocr.bj.bcebos.com/dygraph_v2.0/ch/ch_ppocr_mobile_v2.0_cls_infer.tar",
}


def main():
    root = Path(__file__).resolve().parents[1] / "models" / "paddleocr"
    manifest = {}
    for part, url in MODELS.items():
        folder = root / part
        folder.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=90) as response:
            archive = response.read()
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            for filename in ("inference.pdmodel", "inference.pdiparams", "inference.pdiparams.info"):
                matches = [m for m in bundle.getmembers() if m.isfile() and Path(m.name).name == filename]
                if len(matches) != 1:
                    raise ValueError(f"Unexpected archive contents: {part}/{filename}")
                with bundle.extractfile(matches[0]) as source:
                    (folder / filename).write_bytes(source.read())
        manifest[part] = {"url": url, "archive_sha256": hashlib.sha256(archive).hexdigest()}
        print(f"Installed {part}", flush=True)
    (root / "sources.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
