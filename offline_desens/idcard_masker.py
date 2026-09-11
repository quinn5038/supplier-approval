#!/usr/bin/env python3
"""Fail-closed batch redaction for mainland China Resident Identity Cards."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import platform
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import cv2
import fitz  # PyMuPDF
import numpy as np
from PIL import Image, ImageOps


LOG = logging.getLogger("idcard_masker")
CARD_WIDTH, CARD_HEIGHT = 1712, 1080
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


@dataclass
class OcrItem:
    polygon: np.ndarray
    text: str
    confidence: float

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        points = self.polygon.astype(int)
        return int(points[:, 0].min()), int(points[:, 1].min()), int(points[:, 0].max()), int(points[:, 1].max())


@dataclass
class FieldMatch:
    key: str
    value: str
    box: tuple[int, int, int, int]


@dataclass
class CardResult:
    card_index: int
    side: str
    name: str | None = None
    valid_until: str | None = None
    status: str = ""


@dataclass
class PageResult:
    source: str
    page: int
    cards: list[CardResult]


class CardOcr:
    """PaddleOCR adapter. Models load once per batch."""

    def __init__(self, model_dir: Path | None = None) -> None:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError("缺少 PaddleOCR。请先按 README 安装 paddlepaddle 和 paddleocr。") from exc
        options: dict[str, object] = {"lang": "ch", "use_angle_cls": True, "show_log": False}
        if model_dir:
            model_dir.mkdir(parents=True, exist_ok=True)
            options.update(det_model_dir=str(model_dir / "det"), rec_model_dir=str(model_dir / "rec"), cls_model_dir=str(model_dir / "cls"))
        self.engine = PaddleOCR(**options)

    def read(self, image: np.ndarray) -> list[OcrItem]:
        items: list[OcrItem] = []
        for page in self.engine.ocr(image, cls=True) or []:
            if page:
                for polygon, (text, confidence) in page:
                    items.append(OcrItem(np.asarray(polygon, np.float32), str(text), float(confidence)))
        return items


def normalise_text(value: str) -> str:
    return "".join(char for char in value if not char.isspace() and char not in ":：")


def normalise_validity(value: str) -> str:
    return normalise_text(value).replace("—", "-").replace("–", "-").replace("~", "-")


def is_name_value(value: str) -> bool:
    value = normalise_text(value)
    return 1 < len(value) <= 12 and all("\u3400" <= char <= "\u9fff" or char == "·" for char in value)


def is_validity_value(value: str) -> bool:
    date = r"\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}"
    return bool(re.fullmatch(rf"{date}-(?:{date}|长期)", normalise_validity(value)))


def padded_box(box: tuple[int, int, int, int], width: int, height: int, min_x: int | None = None) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    pad_x, pad_y = max(10, int((x2 - x1) * 0.12)), max(8, int((y2 - y1) * 0.25))
    return max(0 if min_x is None else min_x, x1 - pad_x), max(0, y1 - pad_y), min(width, x2 + pad_x), min(height, y2 + pad_y)


def find_field(
    items: Iterable[OcrItem], key: str, labels: Sequence[str], validator: Callable[[str], bool], width: int, height: int
) -> FieldMatch | None:
    """Locate a value right of its label, including OCR-merged labels."""
    records = list(items)
    for index, label in enumerate(records):
        raw_label = normalise_text(label.text)
        matched = next((candidate for candidate in labels if candidate in raw_label), None)
        if not matched:
            continue
        lx1, ly1, lx2, ly2 = label.bounds
        start = raw_label.find(matched) + len(matched)
        suffix = raw_label[start:]
        if validator(suffix):
            char_width = (lx2 - lx1) / max(1, len(raw_label))
            box = (int(lx1 + char_width * start), ly1, lx2, ly2)
            value = normalise_validity(suffix) if key == "valid_until" else suffix
            return FieldMatch(key, value, padded_box(box, width, height, min_x=box[0]))

        mid_y = (ly1 + ly2) / 2
        same_row = []
        for candidate in records:
            if candidate is label:
                continue
            x1, y1, x2, y2 = candidate.bounds
            if x1 >= lx2 - max(20, (lx2 - lx1) * 0.25) and abs((y1 + y2) / 2 - mid_y) <= max(42, (ly2 - ly1) * 0.9):
                same_row.append(candidate)
        same_row.sort(key=lambda candidate: candidate.bounds[0])
        for end in range(1, len(same_row) + 1):
            value = "".join(normalise_text(candidate.text) for candidate in same_row[:end])
            if validator(value):
                boxes = [candidate.bounds for candidate in same_row[:end]]
                box = min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)
                value = normalise_validity(value) if key == "valid_until" else value
                return FieldMatch(key, value, padded_box(box, width, height, min_x=box[0]))
    return None


def side_scores(items: Iterable[OcrItem]) -> tuple[int, int]:
    text = "".join(normalise_text(item.text) for item in items)
    front = sum(word in text for word in ("姓名", "性别", "民族", "出生", "住址", "公民身份号码"))
    back = sum(word in text for word in ("签发机关", "有效期限", "有效期", "居民身份证"))
    return front, back


def order_quad(points: np.ndarray) -> np.ndarray:
    points = points.astype(np.float32)
    sums, diffs = points.sum(axis=1), np.diff(points, axis=1).reshape(-1)
    quad = np.array([points[np.argmin(sums)], points[np.argmin(diffs)], points[np.argmax(sums)], points[np.argmax(diffs)]], np.float32)
    if np.linalg.norm(quad[1] - quad[0]) < np.linalg.norm(quad[2] - quad[1]):
        quad = np.array([quad[1], quad[2], quad[3], quad[0]], np.float32)
    return quad


def quad_area(quad: np.ndarray) -> float:
    return abs(float(cv2.contourArea(quad.astype(np.float32))))


def candidate_quads(contours: Iterable[np.ndarray], source_area: int) -> list[np.ndarray]:
    candidates: list[np.ndarray] = []
    for contour in contours:
        if cv2.contourArea(contour) < source_area * 0.025:
            continue
        short, long = sorted(cv2.minAreaRect(contour)[1])
        if short < 30 or not 1.30 <= long / short <= 1.90:
            continue
        quad = order_quad(cv2.boxPoints(cv2.minAreaRect(contour)))
        if quad_area(quad) >= source_area * 0.035:
            candidates.append(quad)
    return candidates


def find_cards(image: np.ndarray) -> list[np.ndarray]:
    """Detect cards on photos and on white PDF pages without continuous borders."""
    h, w = image.shape[:2]
    area = h * w
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 130)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    candidates = candidate_quads(contours, area)

    foreground = np.where(gray < 248, 255, 0).astype(np.uint8)
    foreground = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (31, 31)))
    contours, _ = cv2.findContours(foreground, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates.extend(candidate_quads(contours, area))

    selected: list[np.ndarray] = []
    for candidate in sorted(candidates, key=quad_area, reverse=True):
        centre = tuple(np.mean(candidate, axis=0))
        if not any(cv2.pointPolygonTest(existing, centre, False) >= 0 for existing in selected):
            selected.append(candidate)
    return sorted(selected, key=lambda quad: (float(quad[:, 1].mean()), float(quad[:, 0].mean())))


def full_image_card(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    return order_quad(np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32))


def stacked_fallback(image: np.ndarray) -> list[np.ndarray]:
    h, w = image.shape[:2]
    if h / w < 1.05:
        return []
    split = h // 2
    return [
        order_quad(np.array([[0, 0], [w - 1, 0], [w - 1, split - 1], [0, split - 1]], np.float32)),
        order_quad(np.array([[0, split], [w - 1, split], [w - 1, h - 1], [0, h - 1]], np.float32)),
    ]


def split_detected_long(source: np.ndarray, cards: list[np.ndarray]) -> list[np.ndarray]:
    """9/10 方案A：正反面拼图（左右/上下并排）检测不完整时，主动二分整图。

    当 find_cards 只检出整图的一小部分（面积占比 < 50%），且整图是明显超出
    单张身份证比例的长条时（横版宽/高 >= 2，或竖版高/宽 >= 2），说明是两张
    身份证并排拼成的一张图、轮廓检测漏掉了另一张。此时按长宽比主动二分整图，
    让正反面各自独立识别 + 独立打码，避免漏涂敏感区域。
    """
    if not cards:
        return cards
    h, w = source.shape[:2]
    total = sum(quad_area(q) for q in cards)
    if total >= h * w * 0.5:
        return cards  # 检测已覆盖大半，不干预
    if w / h >= 2.0:  # 横版长条：左右并排
        split = w // 2
        return [
            order_quad(np.array([[0, 0], [split - 1, 0], [split - 1, h - 1], [0, h - 1]], np.float32)),
            order_quad(np.array([[split, 0], [w - 1, 0], [w - 1, h - 1], [split, h - 1]], np.float32)),
        ]
    if h / w >= 2.0:  # 竖版长条：上下并排
        split = h // 2
        return [
            order_quad(np.array([[0, 0], [w - 1, 0], [w - 1, split - 1], [0, split - 1]], np.float32)),
            order_quad(np.array([[0, split], [w - 1, split], [w - 1, h - 1], [0, h - 1]], np.float32)),
        ]
    return cards


def card_warp(image: np.ndarray, quad: np.ndarray) -> np.ndarray:
    destination = np.array([[0, 0], [CARD_WIDTH - 1, 0], [CARD_WIDTH - 1, CARD_HEIGHT - 1], [0, CARD_HEIGHT - 1]], np.float32)
    return cv2.warpPerspective(image, cv2.getPerspectiveTransform(quad, destination), (CARD_WIDTH, CARD_HEIGHT))


ROTATIONS: tuple[tuple[int | None, int | None], ...] = (
    (None, None),
    (cv2.ROTATE_180, cv2.ROTATE_180),
    (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE),
    (cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_90_CLOCKWISE),
)


def rotate(image: np.ndarray, code: int | None) -> np.ndarray:
    return image if code is None else cv2.rotate(image, code)


def analyse_card(card: np.ndarray, ocr: CardOcr) -> tuple[str, FieldMatch | None, int | None, int | None]:
    candidates = []
    for forward, backward in ROTATIONS:
        oriented = rotate(card, forward)
        items = ocr.read(oriented)
        height, width = oriented.shape[:2]
        name = find_field(items, "name", ("姓名",), is_name_value, width, height)
        validity = find_field(items, "valid_until", ("有效期限", "有效期"), is_validity_value, width, height)
        front_score, back_score = side_scores(items)
        if name:
            candidates.append((100 + front_score, "front", name, forward, backward))
        elif validity:
            candidates.append((100 + back_score, "back", validity, forward, backward))
        elif front_score > back_score:
            candidates.append((front_score, "front", None, forward, backward))
        elif back_score > front_score:
            candidates.append((back_score, "back", None, forward, backward))
        else:
            candidates.append((0, "unknown", None, forward, backward))
    _, side, field, forward, backward = max(candidates, key=lambda candidate: candidate[0])
    return side, field, forward, backward


def redact_card(source: np.ndarray, quad: np.ndarray, ocr: CardOcr, card_index: int) -> tuple[np.ndarray, np.ndarray, CardResult]:
    card = card_warp(source, quad)
    side, field, forward, backward = analyse_card(card, ocr)
    oriented = rotate(card, forward)
    redacted = np.zeros_like(oriented)
    if field:
        x1, y1, x2, y2 = field.box
        redacted[y1:y2, x1:x2] = oriented[y1:y2, x1:x2]
    redacted = rotate(redacted, backward)
    destination = np.array([[0, 0], [CARD_WIDTH - 1, 0], [CARD_WIDTH - 1, CARD_HEIGHT - 1], [0, CARD_HEIGHT - 1]], np.float32)
    inverse = cv2.getPerspectiveTransform(destination, quad)
    layer = cv2.warpPerspective(redacted, inverse, (source.shape[1], source.shape[0]))
    mask = cv2.warpPerspective(np.full((CARD_HEIGHT, CARD_WIDTH), 255, np.uint8), inverse, (source.shape[1], source.shape[0]), flags=cv2.INTER_NEAREST)
    if side == "front":
        result = CardResult(card_index, side, name=field.value if field else None, status="已脱敏" if field else "未识别姓名，整张正面遮盖")
    elif side == "back":
        result = CardResult(card_index, side, valid_until=field.value if field else None, status="已脱敏" if field else "未识别有效期限，整张背面遮盖")
    else:
        result = CardResult(card_index, side, status="未识别正反面，整张卡片遮盖")
    return layer, mask, result


def process_page(source: np.ndarray, ocr: CardOcr, layout: str, source_name: str, page: int) -> tuple[np.ndarray, PageResult]:
    cards = [full_image_card(source)] if layout == "single" else find_cards(source)
    # 9/11：无清晰矩形边框的上下排身份证，find_cards 可能返回 0；
    # auto 布局也应尝试上下二分兜底（此前仅 stacked 触发，导致「正反面均未识别」）
    if not cards and layout != "single":
        cards = stacked_fallback(source)
    if not cards and 1.30 <= source.shape[1] / source.shape[0] <= 1.90:
        cards = [full_image_card(source)]
    if not cards:
        return np.zeros_like(source), PageResult(source_name, page, [CardResult(1, "unknown", status="未找到卡片，整页遮盖")])
    # 9/10 方案A：正反面拼图（左右/上下并排）检测不完整时，主动二分整图
    if layout != "single":
        cards = split_detected_long(source, cards)
    output, results = source.copy(), []
    for index, quad in enumerate(cards, start=1):
        layer, mask, result = redact_card(source, quad, ocr, index)
        output[mask > 0] = layer[mask > 0]
        results.append(result)
    return output, PageResult(source_name, page, results)


def pil_to_bgr(path: Path) -> np.ndarray:
    with Image.open(path) as opened:
        return cv2.cvtColor(np.asarray(ImageOps.exif_transpose(opened).convert("RGB")), cv2.COLOR_RGB2BGR)


def save_bgr(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)).save(path)


def page_stem(relative: Path, page: int) -> Path:
    return relative.with_suffix("").parent / f"{relative.stem}__page-{page:03d}"


def write_page(output_dir: Path, relative: Path, page: int, image: np.ndarray, result: PageResult, mode: str) -> list[dict[str, str]]:
    stem = page_stem(relative, page)
    if mode in ("images", "both"):
        save_bgr(output_dir / "images" / stem.with_name(f"{stem.name}__masked.png"), image)
    rows: list[dict[str, str]] = []
    if mode in ("fields", "both"):
        path = output_dir / "fields" / stem.with_name(f"{stem.name}__fields.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for card in result.cards:
            rows.append({"source": result.source, "page": str(result.page), "card_index": str(card.card_index), "side": card.side, "name": card.name or "", "valid_until": card.valid_until or "", "status": card.status})
    return rows


def process_pdf(path: Path, relative: Path, output_dir: Path, ocr: CardOcr, layout: str, dpi: int, mode: str) -> list[dict[str, str]]:
    document, rows = fitz.open(path), []
    try:
        for index, page in enumerate(document, start=1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
            image = cv2.imdecode(np.frombuffer(pixmap.tobytes("png"), np.uint8), cv2.IMREAD_COLOR)
            masked, result = process_page(image, ocr, layout, relative.as_posix(), index)
            rows.extend(write_page(output_dir, relative, index, masked, result, mode))
            LOG.info("%s page %03d: %s", relative, index, "; ".join(card.status for card in result.cards))
    finally:
        document.close()
    return rows


def process_image(path: Path, relative: Path, output_dir: Path, ocr: CardOcr, layout: str, mode: str) -> list[dict[str, str]]:
    masked, result = process_page(pil_to_bgr(path), ocr, layout, relative.as_posix(), 1)
    LOG.info("%s: %s", relative, "; ".join(card.status for card in result.cards))
    return write_page(output_dir, relative, 1, masked, result, mode)


def input_files(source_dir: Path) -> list[Path]:
    return sorted(path for path in source_dir.rglob("*") if path.is_file() and (path.suffix.lower() in IMAGE_SUFFIXES or path.suffix.lower() == ".pdf"))


def validate_runtime(parser: argparse.ArgumentParser) -> None:
    """Fail early with an actionable message for unsupported Python/Windows setups."""
    if not (sys.version_info >= (3, 10) and sys.version_info < (3, 14)):
        parser.error("需要 64 位 Python 3.10–3.13；当前版本为 " + platform.python_version())
    if platform.system() == "Windows":
        architecture = platform.machine().lower()
        # platform.machine() 在部分受限运行时（沙箱/CI）会因 PROCESSOR_ARCHITECTURE
        # 环境变量被清除而返回空字符串，此时不应误判为 ARM64，直接放行（x64 与否由
        # 上层 sys.version_info 与后续 paddlepaddle 安装兜底）。
        if architecture and architecture not in {"amd64", "x86_64"}:
            parser.error("Windows 需要 x64/AMD64 Python。PaddlePaddle 当前不提供 Windows ARM64 安装包。")


def main() -> int:
    parser = argparse.ArgumentParser(description="中国大陆二代身份证最小保留式脱敏")
    parser.add_argument("input_dir", type=Path, help="待处理图片/PDF 目录（递归读取）")
    parser.add_argument("output_dir", type=Path, help="输出根目录")
    parser.add_argument("--output-mode", choices=("images", "fields", "both"), default="images", help="默认 images")
    parser.add_argument("--layout", choices=("auto", "stacked", "single"), default="auto", help="默认 auto；stacked 仅在无法检出卡片时上下二分")
    parser.add_argument("--dpi", type=int, default=300, help="PDF 渲染分辨率，默认 300")
    parser.add_argument("--model-dir", type=Path, help="可选：PaddleOCR 模型缓存目录")
    args = parser.parse_args()
    validate_runtime(parser)
    if not args.input_dir.is_dir():
        parser.error(f"输入目录不存在：{args.input_dir}")
    if args.input_dir.resolve() == args.output_dir.resolve():
        parser.error("输入目录与输出目录不能相同")
    files = input_files(args.input_dir)
    if not files:
        parser.error("输入目录中没有支持的图片或 PDF")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        ocr = CardOcr(args.model_dir)
    except Exception as exc:
        LOG.error("无法初始化 OCR：%s", exc)
        LOG.error("请确认已安装 paddlepaddle/paddleocr，且首次模型下载网络可用。")
        return 2
    all_rows: list[dict[str, str]] = []
    failures = 0
    for path in files:
        relative = path.relative_to(args.input_dir)
        try:
            if path.suffix.lower() == ".pdf":
                all_rows.extend(process_pdf(path, relative, args.output_dir, ocr, args.layout, args.dpi, args.output_mode))
            else:
                all_rows.extend(process_image(path, relative, args.output_dir, ocr, args.layout, args.output_mode))
        except Exception as exc:
            failures += 1
            LOG.exception("%s: 失败：%s", relative, exc)
    if args.output_mode in ("fields", "both"):
        csv_path = args.output_dir / "fields" / "fields.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=("source", "page", "card_index", "side", "name", "valid_until", "status"))
            writer.writeheader()
            writer.writerows(all_rows)
    LOG.info("完成：%d 个输入文件；输出：%s", len(files), args.output_dir)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
