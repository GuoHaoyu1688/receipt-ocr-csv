from pathlib import Path

import pytest

from receipt_ocr_csv.core.common import settings
from receipt_ocr_csv.core.store import Store

EXAMPLES = Path(__file__).parents[1] / "examples"


@pytest.fixture
def store(tmp_path):
    root, config = settings(tmp_path / "data")
    instance = Store(root, config)
    yield instance
    instance.close()


@pytest.fixture
def text_pdf():
    return EXAMPLES / "receipts/receipt-09-text.pdf"
