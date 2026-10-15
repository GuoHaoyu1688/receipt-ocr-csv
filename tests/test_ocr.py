import json
import time
from pathlib import Path

import pytest
from PIL import Image

from receipt_ocr_csv.core.common import ReceiptError, settings
from receipt_ocr_csv.core.imaging import decode_document, tesseract

EXAMPLES = Path(__file__).parents[1] / "examples"


@pytest.mark.ocr
def test_ten_actual_receipts_accuracy(store):
    """Acceptance runs the real bundled ONNX models; it never substitutes OCR output."""
    start = time.perf_counter()
    result = store.scan(str(EXAMPLES / "receipts"))
    assert not result["errors"], result["errors"]
    actual = {
        Path(store.show(row["id"])["path"]).name: store.show(row["id"])
        for row in result["processed"]
    }
    truth = json.loads((EXAMPLES / "ground-truth.json").read_text())
    dates = sum(actual[row["file"]]["record"]["date"] == row["date"] for row in truth)
    amounts = sum(actual[row["file"]]["record"]["amount"] == row["amount"] for row in truth)
    assert dates / 10 >= 0.9, actual
    assert amounts / 10 >= 0.9, actual
    assert actual["receipt-07.png"]["record"]["vendor"] == "Example Hotel G"
    assert actual["receipt-09-text.pdf"]["engine"] == "pdf-text"
    assert actual["receipt-10-scan.pdf"]["engine"] == "rapidocr"
    assert actual["receipt-04.png"]["details"]["qr"]
    for item in actual.values():
        for box in item["details"]["boxes"]:
            page = item["details"]["pages"][box["page"] - 1]
            assert 0 <= box["bbox"][0] < box["bbox"][2] <= page["width"] + 1
            assert 0 <= box["bbox"][1] < box["bbox"][3] <= page["height"] + 1
    print(
        json.dumps(
            {
                "date_correct": dates,
                "amount_correct": amounts,
                "total": 10,
                "seconds": round(time.perf_counter() - start, 3),
            }
        )
    )


def test_no_preprocess_and_limits(tmp_path, monkeypatch):
    _, config = settings(tmp_path)
    calls = []
    monkeypatch.setattr(
        "receipt_ocr_csv.core.imaging.rapidocr",
        lambda image, config: calls.append(image.size) or [],
    )
    document = decode_document(
        EXAMPLES / "receipts/receipt-01.png", tmp_path / "pages", "rapidocr", config, False
    )
    assert calls == [(1000, 1100)]
    assert document["pages"][0]["rotation"] == 0
    config["max_pixels"] = 1
    with pytest.raises(ReceiptError):
        decode_document(
            EXAMPLES / "receipts/receipt-01.png", tmp_path / "limits", "rapidocr", config
        )


@pytest.mark.system
def test_optional_tesseract():
    import shutil
    import subprocess

    if not shutil.which("tesseract"):
        pytest.skip("Tesseract is an optional system package")
    languages = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout
    if "eng" not in languages:
        pytest.skip("English Tesseract data is unavailable")
    _, config = settings()
    config["tesseract_language"] = "eng"
    with Image.open(EXAMPLES / "receipts/receipt-01.png") as image:
        assert any("2026" in box["text"] for box in tesseract(image, config))
