from __future__ import annotations

import logging
import sys
import types

import pytest

from ocr_worker import ocr


def test_build_ocr_reader_uses_ppocrv6_small_without_deprecated_args(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakePaddleOCR:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = types.SimpleNamespace(PaddleOCR=FakePaddleOCR)
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)

    paddlex_logger = logging.getLogger("paddlex")
    previous_level = paddlex_logger.level
    try:
        reader = ocr._build_ocr_reader()
        assert paddlex_logger.level == logging.WARNING
    finally:
        paddlex_logger.setLevel(previous_level)

    assert isinstance(reader, FakePaddleOCR)
    assert "ocr_version" not in captured
    assert "lang" not in captured
    assert captured["text_detection_model_name"] == "PP-OCRv6_small_det"
    assert captured["text_recognition_model_name"] == "PP-OCRv6_small_rec"
    assert captured["textline_orientation_model_name"] == "PP-LCNet_x0_25_textline_ori"
    assert captured["device"] == "cpu"
    assert captured["use_doc_orientation_classify"] is False
    assert captured["use_doc_unwarping"] is False
    assert captured["use_textline_orientation"] is True
    deprecated_args = (
        "use_angle_cls",
        "use_gpu",
        "det_db_thresh",
        "det_db_box_thresh",
        "det_db_unclip_ratio",
        "rec_batch_num",
    )
    for deprecated in deprecated_args:
        assert deprecated not in captured


def test_collect_ocr_lines_reads_paddleocr_v3_result_payloads() -> None:
    results = [
        {
            "res": {
                "rec_texts": ["Total USD 12.34", "Coffee Shop", "low confidence"],
                "rec_scores": [0.91, 0.93, 0.39],
                "rec_polys": [
                    [[10, 40], [120, 40], [120, 52], [10, 52]],
                    [[10, 10], [100, 10], [100, 22], [10, 22]],
                    [[10, 70], [100, 70], [100, 82], [10, 82]],
                ],
            }
        }
    ]

    text, confidence = ocr._collect_ocr_lines(results)

    assert text == "Coffee Shop\nTotal USD 12.34"
    assert confidence == pytest.approx(0.92)


def test_collect_ocr_layout_reads_paddleocr_v3_boxes() -> None:
    results = [
        {
            "res": {
                "rec_texts": ["Invoice 77", "Amount 45.00"],
                "rec_scores": [0.9, 0.8],
                "rec_boxes": [
                    [10, 10, 110, 30],
                    [10, 40, 150, 60],
                ],
            }
        }
    ]

    text, confidence, layout = ocr._collect_ocr_layout(results)

    assert text == "Invoice 77\nAmount 45.00"
    assert confidence == pytest.approx(0.85)
    assert [row["text"] for row in layout] == ["Invoice 77", "Amount 45.00"]
    assert all(row["page"] == 1 for row in layout)
    assert layout[0]["x0"] == pytest.approx(0.0)
    assert layout[0]["y0"] == pytest.approx(0.0)
