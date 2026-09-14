"""Exercise installed OCR with generated text and all networking blocked."""
import socket
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from offline_desens.idcard_masker import CardOcr, process_page


def deny_network(*args, **kwargs):
    raise RuntimeError("Network is disabled during local OCR validation")


def main():
    socket.socket.connect = deny_network
    socket.create_connection = deny_network
    root = Path(__file__).resolve().parents[1]
    ocr = CardOcr(root / "models" / "paddleocr")
    sample = np.full((200, 900, 3), 255, np.uint8)
    cv2.putText(sample, "TEST 12345", (45, 120), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 3)
    items = ocr.read(sample)
    if not any("12345" in item.text for item in items):
        raise AssertionError("Installed OCR did not recognise generated text")
    blank = np.full((108, 171, 3), 255, np.uint8)
    masked, result = process_page(blank, ocr, "single", "synthetic", 1)
    assert masked.max() == 0 and result.cards[0].name is None
    print("PASS: real local OCR recognised synthetic text; unreadable card fully masked; network blocked")


if __name__ == "__main__":
    main()
