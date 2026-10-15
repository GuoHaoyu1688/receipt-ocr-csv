"""Generate ten fictional receipts, including rotated, blurred and PDF fixtures."""

import json
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).parent


def font(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def main():
    directory = ROOT / "receipts"
    directory.mkdir(exist_ok=True)
    truth = []
    vendors = [
        "Example Cafe A",
        "Example Transit B",
        "Example Hotel C",
        "Example Office D",
        "Example Cafe E",
        "Example Transit F",
        "Example Hotel G",
        "Example Office H",
        "Example Cafe I",
        "Example Hotel J",
    ]
    for index, vendor in enumerate(vendors, 1):
        total = f"{index * 17 + 24}.50"
        day = f"2026-01-{index:02d}"
        number = f"EXAMPLE{index:04d}"
        rows = [
            "FICTIONAL RECEIPT",
            f"Vendor: {vendor}",
            f"Date: {day}",
            f"Invoice No: {number}",
            "Buyer: Example Co.",
            "Item: Sample service",
            "Tax: CNY 3.50",
            f"TOTAL: CNY {total}",
            "Payment: Card",
            "SYNTHETIC SAMPLE - NOT VALID",
        ]
        image = Image.new("RGB", (1000, 1100), "white")
        draw = ImageDraw.Draw(image)
        for line, text in enumerate(rows):
            draw.text((55, 50 + line * 70), text, fill="#101010", font=font(34 if line else 43))
        if index == 4:
            qr = (
                qrcode.make(f"RECEIPT|date={day}|amount={total}|invoice_no={number}")
                .convert("RGB")
                .resize((260, 260))
            )
            image.paste(qr, (650, 800))
        if index == 7:
            image = image.rotate(90, expand=True)
        if index == 8:
            image = image.filter(ImageFilter.GaussianBlur(1.1))
        name = f"receipt-{index:02d}.png"
        if index == 9:
            name = "receipt-09-text.pdf"
            pdf = canvas.Canvas(str(directory / name), pagesize=(500, 550))
            pdf.setTitle("Fictional example receipt")
            pdf.setAuthor("The receipt-ocr-csv Authors")
            for line, text in enumerate(rows):
                pdf.setFont("Helvetica", 16)
                pdf.drawString(28, 510 - line * 35, text)
            pdf.save()
        elif index == 10:
            name = "receipt-10-scan.pdf"
            image.save(directory / name, "PDF", resolution=144)
        else:
            image.save(directory / name)
        truth.append(
            {
                "file": name,
                "date": day,
                "amount": total,
                "vendor": vendor,
                "invoice_no": number,
                "variation": {
                    7: "rotated 90 degrees",
                    8: "Gaussian blur 1.1",
                    9: "PDF text layer",
                    10: "scanned PDF",
                }.get(index, "clean"),
            }
        )
    (ROOT / "ground-truth.json").write_text(json.dumps(truth, indent=2) + "\n", "utf-8")


if __name__ == "__main__":
    main()
