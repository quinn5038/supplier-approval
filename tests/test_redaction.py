import json

import numpy as np
import pymupdf

from offline_desens import idcard_masker as masker


class EmptyOcr:
    def read(self, image):
        return []


def _item(text, bounds, confidence=0.99):
    x1, y1, x2, y2 = bounds
    return masker.OcrItem(np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
                                   dtype=np.float32), text, confidence)


def test_name_label_confusion_uses_same_row_value_with_front_context():
    items = [
        # Real failure pattern: value is detected before the blue label, and
        # ``姓名`` is recognised as ``城名5`` against the holographic background.
        _item("张洪平", (337, 144, 571, 218)),
        _item("城名5", (151, 156, 370, 224), 0.60),
        _item("民族汉", (400, 284, 737, 365)),
        _item("公民身份号码", (146, 897, 509, 965)),
    ]
    field = masker.find_name_field(items, 1712, 1080)
    assert field is not None
    assert field.value == "张洪平"


def test_name_label_confusion_without_front_context_stays_fail_closed():
    items = [_item("城名5", (100, 100, 250, 160), 0.60),
             _item("敏感文字", (240, 100, 500, 160))]
    assert masker.find_name_field(items, 1712, 1080) is None


def test_low_contrast_front_recovers_name_when_label_becomes_hash():
    items = [
        _item("#测试姓名", (149, 151, 580, 237), 0.91),
        _item("1990年1月1日", (346, 408, 885, 473)),
        _item("示例省示例市测试区", (342, 547, 1024, 602)),
        _item("公民身号110101199001011234", (155, 856, 1493, 922)),
    ]
    field = masker.find_name_field(items, 1712, 1080)
    assert field is not None
    assert field.value == "测试姓名"
    assert masker.horizontal_field(field)


def test_positional_name_recovery_requires_id_number_and_birth_date():
    items = [
        _item("#敏感姓名", (149, 151, 580, 237)),
        _item("1990年1月1日", (346, 408, 885, 473)),
    ]
    assert masker.find_name_field(items, 1712, 1080) is None


def test_unrecognised_image_is_black():
    original = np.full((108, 171, 3), 255, dtype=np.uint8)
    masked, result = masker.process_page(original, EmptyOcr(), "single", "synthetic", 1)
    assert masked.max() == 0
    assert result.cards[0].name is None


def test_background_outside_detected_card_is_black(monkeypatch):
    original = np.full((100, 100, 3), 255, dtype=np.uint8)
    quad = np.array([[10, 10], [90, 10], [90, 70], [10, 70]], dtype=np.float32)
    monkeypatch.setattr(masker, "find_cards", lambda image: [quad])
    monkeypatch.setattr(masker, "split_detected_long", lambda image, cards: cards)
    def fake_redact(*args):
        layer = np.zeros_like(original)
        layer[30:35, 30:50] = 255
        mask = np.zeros((100, 100), dtype=np.uint8)
        mask[10:71, 10:91] = 255
        return layer, mask, masker.CardResult(1, "front", name="合成姓名")
    monkeypatch.setattr(masker, "redact_card", fake_redact)
    result, _ = masker.process_page(original, EmptyOcr(), "auto", "synthetic", 1)
    assert result[0:10].max() == 0
    assert result[30:35, 30:50].min() == 255


def test_blue_card_fallback_ignores_large_red_stamp_and_finds_both_sides(monkeypatch):
    image = np.full((780, 1000, 3), 255, dtype=np.uint8)
    # Pale-blue security backgrounds of the back and front sides.
    cv2 = __import__("cv2")
    cv2.rectangle(image, (250, 80), (750, 395), (235, 215, 185), -1)
    cv2.rectangle(image, (250, 440), (750, 755), (235, 215, 185), -1)
    # A red seal crosses both cards and would otherwise dominate contour detection.
    cv2.circle(image, (700, 415), 180, (40, 40, 210), 24)

    cards = masker.find_blue_card_regions(image)
    assert len(cards) == 2
    assert all(1.3 <= (quad[:, 0].max() - quad[:, 0].min()) /
               (quad[:, 1].max() - quad[:, 1].min()) <= 1.9 for quad in cards)

    bad_stamp_quad = np.array([[650, 160], [870, 500], [650, 650], [430, 310]],
                              dtype=np.float32)
    monkeypatch.setattr(masker, "find_cards", lambda source: [bad_stamp_quad])
    seen = []

    def fake_redact(source, quad, ocr, card_index):
        seen.append(quad)
        return (np.zeros_like(source), np.zeros(source.shape[:2], dtype=np.uint8),
                masker.CardResult(card_index, "front" if card_index == 2 else "back"))

    monkeypatch.setattr(masker, "redact_card", fake_redact)
    _, result = masker.process_page(image, EmptyOcr(), "auto", "stamped.pdf", 1)
    assert len(seen) == 2
    assert [card.side for card in result.cards] == ["back", "front"]


def test_blue_card_fallback_splits_side_by_side_cards_crossed_by_stamp(monkeypatch):
    image = np.full((600, 1300, 3), 255, dtype=np.uint8)
    cv2 = __import__("cv2")
    # Synthetic blue security backgrounds share one vertical band but have a
    # narrow white gap. A seal below the gap makes contour detection return one
    # combined region, matching the production failure without using real IDs.
    cv2.rectangle(image, (80, 80), (630, 425), (235, 215, 185), -1)
    cv2.rectangle(image, (650, 80), (1200, 425), (235, 215, 185), -1)
    cv2.circle(image, (640, 440), 145, (40, 40, 210), 20)

    cards = masker.find_blue_card_regions(image)
    assert len(cards) == 2
    assert cards[0][:, 0].max() < cards[1][:, 0].min()

    combined = np.array([[80, 80], [1200, 80], [1050, 570], [230, 570]],
                        dtype=np.float32)
    monkeypatch.setattr(masker, "find_cards", lambda source: [combined])
    seen = []

    def fake_redact(source, quad, ocr, card_index):
        seen.append(quad)
        side = "front" if card_index == 1 else "back"
        return (np.zeros_like(source), np.zeros(source.shape[:2], dtype=np.uint8),
                masker.CardResult(card_index, side))

    monkeypatch.setattr(masker, "redact_card", fake_redact)
    _, result = masker.process_page(image, EmptyOcr(), "auto", "synthetic", 1)
    assert len(seen) == 2
    assert [card.side for card in result.cards] == ["front", "back"]


def test_wide_low_resolution_scan_falls_back_to_two_horizontal_cards(monkeypatch):
    image = np.full((208, 662, 3), 255, dtype=np.uint8)
    monkeypatch.setattr(masker, "find_cards", lambda source: [])
    # Reproduce a partial colour-background detection: one side is found, but
    # not enough to replace the empty contour result.
    one_side = np.array([[0, 0], [320, 0], [320, 207], [0, 207]], dtype=np.float32)
    monkeypatch.setattr(masker, "find_blue_card_regions", lambda source: [one_side])
    seen = []

    def fake_redact(source, quad, ocr, card_index):
        seen.append(quad)
        side = "back" if card_index == 1 else "front"
        return (np.zeros_like(source), np.zeros(source.shape[:2], dtype=np.uint8),
                masker.CardResult(card_index, side))

    monkeypatch.setattr(masker, "redact_card", fake_redact)
    _, result = masker.process_page(image, EmptyOcr(), "auto", "synthetic", 1)

    assert len(seen) == 2
    assert seen[0][:, 0].max() < seen[1][:, 0].min()
    assert [card.side for card in result.cards] == ["back", "front"]


def test_analyse_card_rejects_vertical_validity_box_from_wrong_rotation():
    horizontal = [
        _item("有效期限2025.07.01-长期", (400, 800, 1150, 880)),
        _item("签发机关", (400, 700, 650, 770)),
        _item("居民身份证", (500, 200, 1100, 330)),
    ]
    vertical_but_more_markers = [
        _item("有效期限2025.07.01-长期", (100, 150, 180, 950)),
        _item("签发机关", (220, 150, 300, 550)),
        _item("居民身份证", (500, 150, 600, 800)),
        _item("有效期", (700, 150, 780, 450)),
    ]

    class RotationOcr:
        def __init__(self):
            self.calls = 0

        def read(self, image):
            values = [horizontal, [], vertical_but_more_markers, []][self.calls]
            self.calls += 1
            return values

    side, field, forward, _ = masker.analyse_card(
        np.full((1080, 1712, 3), 255, dtype=np.uint8), RotationOcr())
    assert side == "back"
    assert field is not None and field.value == "2025.07.01-长期"
    assert masker.horizontal_field(field)
    assert forward is None


def test_two_page_pdf_outputs_both_pages(tmp_path):
    source = tmp_path / "synthetic.pdf"
    with pymupdf.open() as document:
        document.new_page(width=171, height=108)
        document.new_page(width=171, height=108)
        document.save(source)
    output = tmp_path / "output"
    masker.process_pdf(source, source.relative_to(tmp_path), output, EmptyOcr(), "single", 72, "both")
    fields = sorted((output / "fields").glob("*.json"))
    assert len(fields) == 2
    assert [json.loads(p.read_text(encoding="utf-8"))["page"] for p in fields] == [1, 2]
    assert len(list((output / "images").glob("*.png"))) == 2
