import json
from unittest.mock import Mock

import numpy as np
import pymupdf
import pytest

import local_ocr
import textin_pipeline as tp
from offline_desens.idcard_masker import OcrItem
from offline_desens.material_ocr import deny_network, ordered_lines, parse_document


def item(text, x, y, confidence=.99):
    return OcrItem(np.array([[x, y], [x + 80, y], [x + 80, y + 20], [x, y + 20]], np.float32), text, confidence)


def test_reading_order_and_coordinates():
    text, detail = ordered_lines([item("500万元", 110, 50), item("营业执照", 0, 0),
                                 item("注册资本", 0, 51)], 2)
    assert text == "营业执照\n注册资本 500万元"
    assert all(row["page_id"] == 2 for row in detail)
    assert detail[0]["confidence"] == .99
    assert len(detail[0]["position"]) == 4


def test_pdf_page_boundaries(tmp_path):
    source = tmp_path / "two.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.new_page()
        doc.save(source)
    ocr = Mock()
    ocr.read.side_effect = [[item("道路运输经营许可证", 0, 0)], [item("ISO9001", 0, 0)]]
    text, detail = parse_document(source, ocr, dpi=72)
    assert "道路运输" in text
    assert [row["page_id"] for row in detail] == [1, 2]


def test_empty_page_and_unsupported_format_are_manual(tmp_path):
    source = tmp_path / "blank.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(source)
    with pytest.raises(ValueError, match="未识别到文字"):
        parse_document(source, Mock(read=Mock(return_value=[])), dpi=72)
    with pytest.raises(ValueError, match="其他格式转人工"):
        parse_document(tmp_path / "sample.docx", Mock())
    with pytest.raises(PermissionError):
        deny_network()


def test_model_change_invalidates_identity(tmp_path, monkeypatch):
    import desensitize
    monkeypatch.setattr(desensitize, "_PADDLE_MODEL_DIR", str(tmp_path))
    for part in ("det", "rec", "cls"):
        (tmp_path / part).mkdir()
        for name in ("inference.pdmodel", "inference.pdiparams"):
            (tmp_path / part / name).write_bytes(b"model")
    previous = local_ocr.cache_identity()
    (tmp_path / "rec/inference.pdiparams").write_bytes(b"new model")
    assert local_ocr.cache_identity() != previous


def test_pipeline_local_batch_ignores_cloud_cache_and_low_confidence(tmp_path, monkeypatch):
    import desensitize
    monkeypatch.setattr(tp, "BASE", tmp_path)
    monkeypatch.setattr(tp, "FILES_DIR", tmp_path / "raw")
    monkeypatch.setattr(tp, "FILES_DESENS_DIR", tmp_path / "staged")
    monkeypatch.setattr(tp, "RESULTS_FILE", tmp_path / "results.json")
    monkeypatch.setattr(desensitize, "__file__", str(tmp_path / "desensitize.py"))
    monkeypatch.setattr(desensitize, "desensitize_id_cards_with_paddle", lambda *args: {})
    raw = tp.FILES_DIR / "1"
    raw.mkdir(parents=True)
    for name in ("a.pdf", "b.pdf"):
        (raw / name).write_bytes(b"synthetic")
    (tmp_path / "cache_v4.json").write_text(json.dumps({"1": {"materials_detail": [
        {"fileName": name, "types": ["iso9001"]} for name in ("a.pdf", "b.pdf")]}}), encoding="utf-8")
    staged = tp.FILES_DESENS_DIR / "1"
    staged.mkdir(parents=True)
    (staged / "a.pdf.md").write_text("old TextIn result")
    batch = Mock(side_effect=lambda paths: {str(path.resolve()): {
        "markdown": "质量管理体系认证证书 ISO9001 有效期至：2099年12月31日",
        "detail": [{"text": "ISO9001", "page_id": 1, "confidence": .7}]} for path in paths})
    monkeypatch.setattr(local_ocr, "parse_files", batch)
    cloud = Mock(side_effect=AssertionError("cloud disabled"))
    monkeypatch.setattr(tp.requests, "post", cloud)
    tp.run_parse("1")
    batch.assert_called_once()
    assert len(batch.call_args.args[0]) == 2
    result = json.loads(tp.RESULTS_FILE.read_text(encoding="utf-8"))["1"]["iso9001"]
    assert None in result["checks"].values()
    assert all(doc["ocr_engine"] == "paddleocr-local" for doc in result["documents"])
    tp.run_parse("1")
    batch.assert_called_once()
    cloud.assert_not_called()
    with pytest.raises(PermissionError, match="禁用 TextIn"):
        tp.parse_file_textin(staged / "a.pdf", "iso9001")
