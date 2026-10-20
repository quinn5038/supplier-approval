"""Validate the main runtime and isolated worker together using a synthetic PDF."""
import sys
import tempfile
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_ocr
import textin_pipeline  # noqa: F401 -- load existing local deployment configuration


def main():
    with tempfile.TemporaryDirectory(prefix="offline-adapter-check-") as folder:
        source = Path(folder) / "synthetic.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((40, 80), "CERTIFICATE 12345", fontsize=24)
            page.insert_text((40, 140), "VALID UNTIL 2099-12-31", fontsize=24)
            doc.save(source)
        result = local_ocr.parse_files([source])[str(source.resolve())]
        assert not result.get("error"), result
        assert "12345" in result["markdown"] and "2099-12-31" in result["markdown"], result
    print("PASS: main runtime -> network-blocked worker -> real model -> structured result")


if __name__ == "__main__":
    main()
