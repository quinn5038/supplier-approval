"""Local document OCR worker; communicates through JSON files, never a cloud API."""
import argparse
import json
import re
import socket
import sys
from pathlib import Path

import cv2
import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from offline_desens.idcard_masker import CardOcr

_COMPLETE_DATE = re.compile(
    r"(?:19|20)\d{2}\s*[年.\-/]\s*\d{1,2}\s*[月.\-/]\s*\d{1,2}(?!\d)")


def _complete_date_count(items):
    return sum(len(_COMPLETE_DATE.findall(str(item.text))) for item in items)


def _has_red_seal(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    red = (((hsv[:, :, 0] <= 10) | (hsv[:, :, 0] >= 170))
           & (hsv[:, :, 1] >= 60) & (hsv[:, :, 2] >= 80))
    return float(red.mean()) >= .001


def read_with_red_seal_retry(image, ocr):
    """Retry seal-covered validity rows through the red channel.

    Red stamp pixels are bright in the red channel while black printed text stays
    dark. Use the retry only when normal OCR saw an effective-date label and the
    retry recovers more complete dates; otherwise preserve the original OCR.
    """
    original = ocr.read(image)
    original_text = "".join(str(item.text).replace(" ", "") for item in original)
    if (not _has_red_seal(image)
            or not re.search(r"有效(?:期|日期)", original_text)):
        return original
    red_channel = cv2.cvtColor(image[:, :, 2], cv2.COLOR_GRAY2BGR)
    retried = ocr.read(red_channel)
    if _complete_date_count(retried) > _complete_date_count(original):
        return retried
    return original


def deny_network(*args, **kwargs):
    raise PermissionError("材料 OCR 进程禁止联网")


def ordered_lines(items, page_id):
    """Group near-horizontal boxes into rows without discarding confidence."""
    boxes = []
    for item in items:
        polygon = np.asarray(item.polygon)
        x, y = polygon.min(axis=0)
        right, bottom = polygon.max(axis=0)
        boxes.append((float(y), float(x), float(bottom - y), item, polygon))
    boxes.sort(key=lambda box: (box[0], box[1]))
    rows = []
    for box in boxes:
        if rows and abs(box[0] - rows[-1][0][0]) <= max(3, min(box[2], rows[-1][0][2]) * .45):
            rows[-1].append(box)
        else:
            rows.append([box])
    text, detail = [], []
    for row in rows:
        row.sort(key=lambda box: box[1])
        text.append(" ".join(box[3].text for box in row))
        for _, _, _, item, polygon in row:
            detail.append({"page_id": page_id, "text": item.text,
                           "position": polygon.tolist(), "confidence": item.confidence})
    return "\n".join(text), detail


def parse_document(path, ocr, dpi=200):
    path = Path(path)
    pages, details = [], []
    if path.suffix.lower() == ".pdf":
        with pymupdf.open(path) as document:
            if document.page_count > 100:
                raise ValueError("材料超过100页，请拆分后核验")
            for number, page in enumerate(document, 1):
                # Rasterize even text PDFs: use one consistent coordinate system.
                pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
                rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)
                image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                text, detail = ordered_lines(read_with_red_seal_retry(image, ocr), number)
                if not detail:
                    raise ValueError(f"第{number}页未识别到文字，转人工")
                pages.append(text)
                details.extend(detail)
    elif path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}:
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("无法读取材料图片")
        text, details = ordered_lines(read_with_red_seal_retry(image, ocr), 1)
        pages.append(text)
    else:
        raise ValueError("第一阶段仅支持 PDF、PNG、JPEG、BMP；其他格式转人工")
    if not details:
        raise ValueError("未识别到文字，转人工")
    return "\n\n".join(pages), details


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    parser.add_argument("response", type=Path)
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args()
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.create_connection = deny_network
    ocr = CardOcr(args.model_dir)
    results = {}
    for path in json.loads(args.request.read_text(encoding="utf-8")):
        try:
            text, detail = parse_document(path, ocr)
            results[path] = {"markdown": text, "detail": detail}
        except Exception as exc:
            results[path] = {"error": str(exc)}
    args.response.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
