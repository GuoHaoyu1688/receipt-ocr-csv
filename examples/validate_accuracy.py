"""Measure real OCR against the ten fictional source receipts in a fresh directory."""

import json
import platform
import tempfile
import time
from pathlib import Path

from receipt_ocr_csv.core.common import settings
from receipt_ocr_csv.core.store import Store


def main():
    examples = Path(__file__).parent
    truth = json.loads((examples / "ground-truth.json").read_text())
    with tempfile.TemporaryDirectory() as directory:
        root, config = settings(Path(directory).resolve())
        store = Store(root, config)
        start = time.perf_counter()
        result = store.scan(str(examples / "receipts"))
        elapsed = time.perf_counter() - start
        if result["errors"]:
            raise RuntimeError(result["errors"])
        rows = {
            Path(store.show(item["id"])["path"]).name: store.show(item["id"])
            for item in result["processed"]
        }
        measures = []
        for sample in truth:
            actual = rows[sample["file"]]
            measures.append(
                {
                    "file": sample["file"],
                    "variation": sample["variation"],
                    "date_correct": actual["record"]["date"] == sample["date"],
                    "amount_correct": actual["record"]["amount"] == sample["amount"],
                    "vendor_correct": actual["record"]["vendor"] == sample["vendor"],
                    "engine": actual["engine"],
                    "rotation": actual["details"]["pages"][0]["rotation"],
                }
            )
        store.close()
    report = {
        "python": platform.python_version(),
        "platform": platform.system() + " " + platform.machine(),
        "engine": "rapidocr-onnxruntime 1.4.4",
        "samples": 10,
        "seconds": round(elapsed, 3),
        "date_accuracy": sum(row["date_correct"] for row in measures) / 10,
        "amount_accuracy": sum(row["amount_correct"] for row in measures) / 10,
        "fixtures": measures,
        "scope": "Actual local OCR on ten generated fictional samples; not real-world accuracy.",
    }
    assert report["date_accuracy"] >= 0.9 and report["amount_accuracy"] >= 0.9
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
