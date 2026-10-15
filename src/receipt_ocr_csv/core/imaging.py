"""Local image/PDF decoding, preprocessing and pluggable OCR adapters."""

import csv
import io
import subprocess
import threading
from pathlib import Path

from PIL import Image, ImageOps

from .common import ReceiptError

_ENGINE = None
_LOCK = threading.Lock()


def preprocess(image: Image.Image) -> Image.Image:
    """Deskew text lines, crop blank borders, normalize shadows and apply Otsu thresholding."""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, 70, minLineLength=80, maxLineGap=15)
    if lines is not None:
        angles = [
            np.degrees(np.arctan2(y2 - y1, x2 - x1)) for x1, y1, x2, y2 in lines.reshape(-1, 4)
        ]
        angles = [angle for angle in angles if abs(angle) < 15]
        if angles:
            angle = float(np.median(angles))
            if abs(angle) > 0.3:
                h, w = gray.shape
                transform = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1)
                gray = cv2.warpAffine(gray, transform, (w, h), borderValue=255)
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, np.ones((31, 31), np.uint8))
    normalized = cv2.divide(gray, background, scale=255)
    _, binary = cv2.threshold(normalized, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    points = cv2.findNonZero(255 - binary)
    if points is not None:
        x, y, w, h = cv2.boundingRect(points)
        padding = 24
        binary = binary[
            max(0, y - padding) : y + h + padding, max(0, x - padding) : x + w + padding
        ]
    return Image.fromarray(binary).convert("RGB")


def rapidocr(image: Image.Image, config: dict) -> list[dict]:
    global _ENGINE
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    with _LOCK:
        if _ENGINE is None:
            _ENGINE = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
        result, _ = _ENGINE(np.asarray(image))
    boxes = []
    for polygon, text, score in result or []:
        xs, ys = zip(*polygon)
        boxes.append(
            {
                "text": text,
                "confidence": float(score),
                "bbox": [float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))],
            }
        )
    return boxes


def tesseract(image: Image.Image, config: dict) -> list[dict]:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", config["tesseract_language"], "tsv"],
            input=buffer.getvalue(),
            capture_output=True,
            timeout=config["ocr_timeout"],
            check=False,
        )
    except FileNotFoundError:
        raise ReceiptError(
            "MISSING_DEPENDENCY", "Install Tesseract and configured language data", 1
        ) from None
    except subprocess.TimeoutExpired:
        raise ReceiptError("OCR_TIMEOUT", "Tesseract exceeded the configured timeout", 1) from None
    if result.returncode:
        raise ReceiptError("OCR_FAILED", result.stderr.decode("utf-8", "replace")[-1000:], 1)
    grouped = {}
    for row in csv.DictReader(io.StringIO(result.stdout.decode("utf-8")), delimiter="\t"):
        if not row["text"].strip() or float(row["conf"]) < 0:
            continue
        key = (row["page_num"], row["block_num"], row["par_num"], row["line_num"])
        grouped.setdefault(key, []).append(row)
    boxes = []
    for rows in grouped.values():
        boxes.append(
            {
                "text": " ".join(row["text"] for row in rows),
                "confidence": sum(float(row["conf"]) for row in rows) / len(rows) / 100,
                "bbox": [
                    min(int(row["left"]) for row in rows),
                    min(int(row["top"]) for row in rows),
                    max(int(row["left"]) + int(row["width"]) for row in rows),
                    max(int(row["top"]) + int(row["height"]) for row in rows),
                ],
            }
        )
    return boxes


def merge_lines(boxes: list[dict]) -> list[dict]:
    """Join horizontally adjacent labels and values on the same baseline."""
    lines = []
    for box in sorted(boxes, key=lambda b: (b["bbox"][1], b["bbox"][0])):
        y = (box["bbox"][1] + box["bbox"][3]) / 2
        target = next(
            (
                line
                for line in lines
                if abs(line["y"] - y) < min(line["h"], box["bbox"][3] - box["bbox"][1]) * 0.45
            ),
            None,
        )
        if target:
            target["boxes"].append(box)
        else:
            lines.append({"y": y, "h": box["bbox"][3] - box["bbox"][1], "boxes": [box]})
    merged = []
    for line in lines:
        parts = sorted(line["boxes"], key=lambda b: b["bbox"][0])
        merged.append(
            {
                "text": " ".join(p["text"] for p in parts),
                "confidence": min(p["confidence"] for p in parts),
                "bbox": [
                    min(p["bbox"][0] for p in parts),
                    min(p["bbox"][1] for p in parts),
                    max(p["bbox"][2] for p in parts),
                    max(p["bbox"][3] for p in parts),
                ],
            }
        )
    return merged


def quality(boxes: list[dict]) -> float:
    from .extraction import normalized_date

    score = sum(len(b["text"]) * b["confidence"] for b in boxes) + len(boxes) * 20
    if any(normalized_date(b["text"]) for b in boxes):
        score += 150
    return score


def recognize(
    image: Image.Image, engine: str, config: dict, use_preprocess: bool
) -> tuple[Image.Image, list, int]:
    adapter = {"rapidocr": rapidocr, "tesseract": tesseract}[engine]
    image = ImageOps.exif_transpose(image).convert("RGB")
    if use_preprocess:
        image = preprocess(image)
    raw_boxes = adapter(image, config)
    boxes = merge_lines(raw_boxes)
    rotation = 0
    if use_preprocess:
        # A date is a useful orientation anchor. Retry cardinal rotations when it is absent.
        from .extraction import normalized_date

        vertical = sum(b["bbox"][3] - b["bbox"][1] > b["bbox"][2] - b["bbox"][0] for b in raw_boxes)
        if vertical > len(raw_boxes) / 2 or not any(normalized_date(box["text"]) for box in boxes):
            best = quality(boxes)
            original = image
            for angle in [90, 180, 270]:
                candidate = original.rotate(angle, expand=True, fillcolor="white")
                found = merge_lines(adapter(candidate, config))
                score = quality(found)
                if score > best:
                    image, boxes, rotation, best = candidate, found, angle, score
    return image, boxes, rotation


def decode_qr(image: Image.Image, page: int) -> list[dict]:
    import cv2
    import numpy as np

    try:
        found, payloads, points, _ = cv2.QRCodeDetector().detectAndDecodeMulti(np.asarray(image))
        if not found:
            return []
        return [
            {
                "payload": payload,
                "page": page,
                "bbox": [
                    float(poly[:, 0].min()),
                    float(poly[:, 1].min()),
                    float(poly[:, 0].max()),
                    float(poly[:, 1].max()),
                ],
            }
            for payload, poly in zip(payloads, points)
            if payload
        ]
    except cv2.error:
        return []


def decode_document(
    path: Path, out: Path, engine: str, config: dict, use_preprocess: bool = True
) -> dict:
    """Prefer the PDF text layer per page; OCR only pages without usable text."""
    if engine not in {"rapidocr", "tesseract"}:
        raise ReceiptError("INVALID_ENGINE", "Engine must be rapidocr or tesseract")
    out.mkdir(parents=True, exist_ok=True)
    boxes, pages, qrs = [], [], []

    def add(image, number, text_boxes=None):
        if image.width * image.height > config["max_pixels"]:
            raise ReceiptError("IMAGE_TOO_LARGE", "Page exceeds configured pixel limit")
        rotation = 0
        method = "pdf-text" if text_boxes else engine
        if text_boxes:
            found = text_boxes
        else:
            image, found, rotation = recognize(image, engine, config, use_preprocess)
        for box in found:
            box["page"] = number
        boxes.extend(found)
        qrs.extend(decode_qr(image, number))
        image.save(out / f"page-{number}.png")
        pages.append(
            {
                "page": number,
                "width": image.width,
                "height": image.height,
                "engine": method,
                "rotation": rotation,
                "preview": f"page-{number}.png",
            }
        )

    if path.suffix.lower() == ".pdf":
        import pdfplumber
        import pypdfium2 as pdfium

        with pdfplumber.open(path) as pdf, pdfium.PdfDocument(path) as rendered:
            if len(pdf.pages) > config["max_pages"]:
                raise ReceiptError("TOO_MANY_PAGES", "PDF exceeds configured page limit")
            for number, page in enumerate(pdf.pages, 1):
                scale = 2.0
                if page.width * page.height * scale**2 > config["max_pixels"]:
                    raise ReceiptError(
                        "IMAGE_TOO_LARGE", "Rendered PDF exceeds configured pixel limit"
                    )
                image = rendered[number - 1].render(scale=scale).to_pil().convert("RGB")
                words = page.extract_words()
                text_boxes = []
                if sum(len(word["text"]) for word in words) >= 20:
                    text_boxes = merge_lines(
                        [
                            {
                                "text": word["text"],
                                "confidence": 1.0,
                                "bbox": [
                                    word["x0"] * scale,
                                    word["top"] * scale,
                                    word["x1"] * scale,
                                    word["bottom"] * scale,
                                ],
                            }
                            for word in words
                        ]
                    )
                add(image, number, text_boxes)
    else:
        if path.suffix.lower() in {".heic", ".heif"}:
            try:
                from pillow_heif import register_heif_opener

                register_heif_opener()
            except ImportError:
                raise ReceiptError(
                    "MISSING_DEPENDENCY", "Install receipt-ocr-csv[heic] for HEIC input", 1
                ) from None
        with Image.open(path) as image:
            if image.width * image.height > config["max_pixels"]:
                raise ReceiptError("IMAGE_TOO_LARGE", "Image exceeds configured pixel limit")
            add(image.copy(), 1)
    return {"boxes": boxes, "pages": pages, "qr": qrs}
