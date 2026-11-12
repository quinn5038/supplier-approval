"""Local document OCR worker; communicates through JSON files, never a cloud API."""
import argparse
import gc
import json
import re
import socket
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from offline_desens.idcard_masker import CardOcr

_COMPLETE_DATE = re.compile(
    r"(?:19|20)\d{2}\s*[年.\-/]\s*\d{1,2}\s*[月.\-/]\s*\d{1,2}(?!\d)")
_LICENSE_ORIENTATION_MARKERS = (
    "营业执照", "统一社会信用代码", "法定代表人", "注册资本", "经营范围", "登记机关")
_ISO_TITLE = re.compile(
    r"(?:质量|环境|职业健康安全)管理体系(?:认证(?:合格)?证书)?|"
    r"(?:QUALITY|ENVIRONMENTAL|OCCUPATIONALHEALTHANDSAFETY)MANAGEMENTSYSTEMCERTIFICATE",
    re.I,
)
_ISO_STANDARD = re.compile(r"(?:ISO|IS0|1SO|1S0)(?:9001|14001|45001)(?!\d)", re.I)
_ISO_NUMBER_LABEL = re.compile(
    r"证书(?:注册)?(?:编号|号)|注册号|(?:CERTIFICATE|REGISTRATION)(?:NO\.?|NUMBER)", re.I)
_ISO_EXPIRY_LABEL = re.compile(r"有效期至|证书有效(?:期|日期)|到期日期|VALIDUNTIL|EXPIRYDATE", re.I)


def _complete_date_count(items):
    return sum(len(_COMPLETE_DATE.findall(str(item.text))) for item in items)


def _has_red_seal(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    red = (((hsv[:, :, 0] <= 10) | (hsv[:, :, 0] >= 170))
           & (hsv[:, :, 1] >= 60) & (hsv[:, :, 2] >= 80))
    return float(red.mean()) >= .001


def _orientation_score(image, items):
    """Rank whole-page orientations using licence layout and box geometry."""
    height = max(1, image.shape[0])
    compact = "".join(str(item.text).replace(" ", "") for item in items)
    marker_count = sum(marker in compact for marker in _LICENSE_ORIENTATION_MARKERS)
    horizontal_chars = total_chars = confidence_chars = 0.0
    layout_score = 0
    for item in items:
        polygon = np.asarray(item.polygon)
        width = float(polygon[:, 0].max() - polygon[:, 0].min())
        box_height = float(polygon[:, 1].max() - polygon[:, 1].min())
        chars = max(1, len(str(item.text).replace(" ", "")))
        total_chars += chars
        confidence_chars += chars * float(item.confidence)
        if width >= box_height:
            horizontal_chars += chars
        text = str(item.text).replace(" ", "")
        center_y = float(polygon[:, 1].mean()) / height
        if any(marker in text for marker in ("营业执照", "统一社会信用代码")):
            layout_score += 1 if center_y < .55 else -1
        if "登记机关" in text:
            layout_score += 1 if center_y > .45 else -1
    horizontal_ratio = horizontal_chars / max(1.0, total_chars)
    # A sideways page can still recognise more field labels than either rotated
    # retry.  Marker count used to win before geometry, so that vertical OCR was
    # incorrectly selected and columns were flattened into one scope value.
    # First require a predominantly horizontal candidate; marker/layout evidence
    # then chooses between the two horizontal (upright/upside-down) rotations.
    return (horizontal_ratio >= .65, marker_count, layout_score,
            horizontal_ratio, confidence_chars / max(1.0, total_chars))


def _iso_orientation_score(image, items):
    """Prefer horizontal text with the certificate heading above its field rows."""
    horizontal_chars = total_chars = confidence_chars = 0
    titles, fields = [], []
    for item in items:
        text = "".join(str(item.text).split())
        polygon = np.asarray(item.polygon)
        width, height = polygon.max(axis=0) - polygon.min(axis=0)
        chars = len(text)
        total_chars += chars
        confidence_chars += chars * float(item.confidence)
        if width >= height:
            horizontal_chars += chars
        y = float(polygon[:, 1].mean())
        if _ISO_TITLE.search(text):
            titles.append(y)
        if _ISO_NUMBER_LABEL.search(text) or _ISO_EXPIRY_LABEL.search(text):
            fields.append(y)
    ratio = horizontal_chars / max(1, total_chars)
    title_above_fields = bool(titles and fields and min(titles) < min(fields))
    title_near_top = bool(titles and min(titles) < image.shape[0] * .5)
    return (ratio >= .65, title_above_fields, title_near_top, bool(titles), len(fields),
            _complete_date_count(items), total_chars, confidence_chars / max(1, total_chars))


def read_with_orientation_retry(image, ocr, *, iso_hint=False):
    """Rotate sideways licences and ISO certificates before arranging OCR lines."""
    original = ocr.read(image)
    compact = "".join("".join(str(item.text).split()) for item in original)
    looks_like_iso = iso_hint or _ISO_TITLE.search(compact) or _ISO_STANDARD.search(compact)
    looks_like_sideways_license = (
        image.shape[0] > image.shape[1] * 1.15
        and ("营业执照" in compact or ("统一社会信用代码" in compact and not looks_like_iso))
    )
    if looks_like_sideways_license:
        score = _orientation_score
    elif looks_like_iso:
        score = _iso_orientation_score
        if score(image, original)[0]:
            return image, original
    else:
        return image, original

    candidates = [(image, original)]
    for code in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
        rotated = cv2.rotate(image, code)
        candidates.append((rotated, ocr.read(rotated)))
    oriented, result = max(candidates, key=lambda candidate: score(*candidate))
    if score is _iso_orientation_score and original and not score(oriented, result)[0]:
        return image, original
    return oriented, result


def read_with_red_seal_retry(image, ocr, original=None):
    """Retry seal-covered validity rows through the red channel.

    Red stamp pixels are bright in the red channel while black printed text stays
    dark. Use the retry only when normal OCR saw an effective-date label and the
    retry recovers more complete dates; otherwise preserve the original OCR.
    """
    original = ocr.read(image) if original is None else original
    original_text = "".join(str(item.text).replace(" ", "") for item in original)
    if (not _has_red_seal(image)
            or not re.search(r"有效(?:期|日期)", original_text)):
        return original
    red_channel = cv2.cvtColor(image[:, :, 2], cv2.COLOR_GRAY2BGR)
    retried = ocr.read(red_channel)
    if _complete_date_count(retried) > _complete_date_count(original):
        return retried
    return original


def read_material_image(image, ocr, *, iso_hint=False):
    oriented, original = read_with_orientation_retry(image, ocr, iso_hint=iso_hint)
    return read_with_red_seal_retry(oriented, ocr, original)


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


def parse_document(path, ocr, dpi=200, metrics=None):
    path = Path(path)
    iso_hint = bool(_ISO_TITLE.search(path.stem) or re.search(r"(?<!\d)(?:9001|14001|45001)(?!\d)", path.stem))
    pages, details = [], []
    metrics = metrics if metrics is not None else {}
    metrics.setdefault("pages", 0)
    metrics.setdefault("max_pixels", 0)
    if path.suffix.lower() == ".pdf":
        with pymupdf.open(path) as document:
            if document.page_count > 100:
                raise ValueError("材料超过100页，请拆分后核验")
            for number, page in enumerate(document, 1):
                # Rasterize even text PDFs: use one consistent coordinate system.
                pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
                rgb = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)
                image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                metrics["pages"] += 1
                metrics["max_pixels"] = max(
                    metrics["max_pixels"], image.shape[0] * image.shape[1])
                text, detail = ordered_lines(read_material_image(image, ocr, iso_hint=iso_hint), number)
                if not detail:
                    raise ValueError(f"第{number}页未识别到文字，转人工")
                pages.append(text)
                details.extend(detail)
    elif path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}:
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("无法读取材料图片")
        metrics["pages"] += 1
        metrics["max_pixels"] = image.shape[0] * image.shape[1]
        text, details = ordered_lines(read_material_image(image, ocr, iso_hint=iso_hint), 1)
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
    model_started = time.perf_counter()
    ocr = CardOcr(args.model_dir)
    model_load_ms = round((time.perf_counter() - model_started) * 1000)
    results = {}
    total_pages = total_ocr_ms = max_pixels = 0
    for path in json.loads(args.request.read_text(encoding="utf-8")):
        started = time.perf_counter()
        metrics = {}
        try:
            text, detail = parse_document(path, ocr, metrics=metrics)
            results[path] = {"markdown": text, "detail": detail}
        except Exception as exc:
            results[path] = {"error": str(exc)}
        finally:
            total_ocr_ms += round((time.perf_counter() - started) * 1000)
            total_pages += metrics.get("pages", 0)
            max_pixels = max(max_pixels, metrics.get("max_pixels", 0))
            # Paddle/OpenCV may retain large page arrays until cyclic GC runs.
            # A collection boundary per document lowers peak usage in long batches.
            gc.collect()
    results["__worker_meta__"] = {
        "model_load_ms": model_load_ms,
        "ocr_ms": total_ocr_ms,
        "pages": total_pages,
        "max_page_pixels": max_pixels,
    }
    args.response.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
