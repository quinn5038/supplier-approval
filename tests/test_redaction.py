import json

import numpy as np
import pymupdf

from offline_desens import idcard_masker as masker


class EmptyOcr:
    def read(self, image):
        return []


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
