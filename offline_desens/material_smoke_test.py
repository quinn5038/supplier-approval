"""Real-model smoke test with generated documents and outbound sockets disabled."""
import argparse
import socket
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from offline_desens.idcard_masker import CardOcr
from offline_desens.material_ocr import deny_network, parse_document


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args()
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.create_connection = deny_network
    start = time.monotonic()
    ocr = CardOcr(args.model_dir)
    image = np.full((500, 1400, 3), 255, np.uint8)
    for y, text in ((100, "ISO9001"), (220, "VALID UNTIL 2099-12-31"), (340, "TEST 12345")):
        cv2.putText(image, text, (45, y), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 3)
    with tempfile.TemporaryDirectory(prefix="material-ocr-test-") as folder:
        png = Path(folder) / "certificate.png"
        cv2.imwrite(str(png), image)
        pdf = Path(folder) / "certificate.pdf"
        with pymupdf.open() as document:
            for _ in range(2):
                page = document.new_page(width=700, height=250)
                page.insert_image(page.rect, filename=str(png))
            document.save(pdf)
        for source in (png, pdf):
            text, detail = parse_document(source, ocr)
            compact = text.replace(" ", "")
            assert "9001" in compact and "2099-12-31" in compact and "12345" in compact, text
            assert all("confidence" in row and "position" in row for row in detail)
            if source == pdf:
                assert {row["page_id"] for row in detail} == {1, 2}
    print(f"PASS: real local OCR recognised PNG and two-page PDF; network blocked; {time.monotonic() - start:.1f}s")


if __name__ == "__main__":
    main()
