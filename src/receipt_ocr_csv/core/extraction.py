"""Conservative bilingual receipt rules with source boxes and QR cross-checks."""

import re
from datetime import date

from .common import ReceiptError, cents, money

NAMES = [
    "date",
    "amount",
    "tax",
    "currency",
    "vendor",
    "buyer",
    "invoice_no",
    "invoice_code",
    "check_code",
    "payment_method",
    "category",
    "note",
]
DEFAULT_CATEGORIES = {
    "Transport": ["taxi", "train", "transit", "交通", "出租", "火车", "车票"],
    "Lodging": ["hotel", "lodging", "inn", "住宿", "酒店", "宾馆"],
    "Meals": ["cafe", "meal", "restaurant", "dining", "餐饮", "餐厅", "饭店"],
    "Office": ["office", "stationery", "paper", "办公", "文具"],
}
DATE_PATTERN = r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})日?"
AMOUNT_PATTERN = r"[-+]?\d[\d,]*\.\d{2}(?!\d)"


def normalized_date(text: str) -> str | None:
    match = re.search(DATE_PATTERN, text)
    if not match:
        compact = re.fullmatch(r"(20\d{2})(\d{2})(\d{2})", text.strip())
        match = compact
    if match:
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            return None
    return None


def parse_qr(payload: str) -> dict:
    """Parse invoice CSV QR payloads or the documented local RECEIPT key-value format."""
    result = {}
    if payload.startswith("RECEIPT|"):
        pairs = dict(item.split("=", 1) for item in payload.split("|")[1:] if "=" in item)
        for key in ["date", "amount", "invoice_no", "invoice_code"]:
            if key in pairs:
                result[key] = pairs[key]
    else:
        parts = payload.split(",")
        if len(parts) >= 6 and parts[0] == "01":
            result = {
                "invoice_code": parts[2],
                "invoice_no": parts[3],
                "net_amount": parts[4],
                "date": parts[5],
            }
            if len(parts) >= 7:
                result["check_code"] = parts[6]
    if "date" in result:
        result["date"] = normalized_date(result["date"])
    for key in ["amount", "net_amount"]:
        if key in result:
            try:
                result[key] = money(cents(result[key]))
            except ReceiptError:
                result.pop(key)
    return {key: value for key, value in result.items() if value}


def extract_fields(
    boxes: list[dict], qrs: list[dict], categories: dict | None = None
) -> tuple[dict, list]:
    """Extract labeled fields, leaving ambiguous amounts blank for review."""
    fields = {
        key: {"value": "", "confidence": 0.0, "sources": [], "edited": False} for key in NAMES
    }
    warnings = []

    def put(key: str, value: str, box: dict, confidence: float = 0.97):
        if value and not fields[key]["value"]:
            fields[key] = {
                "value": value.strip(),
                "confidence": round(min(confidence, box.get("confidence", 1)), 4),
                "sources": [{"page": box["page"], "bbox": box["bbox"], "text": box["text"]}],
                "edited": False,
            }

    for box in boxes:
        text = box["text"].strip()
        candidate = normalized_date(text)
        if candidate:
            put("date", candidate, box, 0.98 if re.search(r"date|日期", text, re.I) else 0.8)
        labels = {
            "invoice_no": (
                r"(?:invoice\s*(?:no\.?|number)|receipt\s*(?:no\.?|number)"
                r"|发票号码|票据号码|票号)\s*[:：#]?\s*([A-Za-z0-9-]{4,30})"
            ),
            "invoice_code": r"(?:invoice\s*code|发票代码)\s*[:：]?\s*(\d{6,20})",
            "check_code": r"(?:check\s*code|校验码)\s*[:：]?\s*([\d ]{6,30})",
            "vendor": (
                r"(?:vendor|merchant|seller|销售方(?:名称)?|商户(?:名称)?"
                r"|销方名称)\s*[:：]\s*(.+)"
            ),
            "buyer": r"(?:buyer|purchaser|购买方(?:名称)?|购方名称)\s*[:：]\s*(.+)",
            "payment_method": r"(?:payment(?:\s*method)?|支付方式|付款方式)\s*[:：]\s*(.+)",
        }
        for key, pattern in labels.items():
            match = re.search(pattern, text, re.I)
            if match:
                put(key, match.group(1), box)
        if re.search(
            r"(?<!sub)(?<!net )\b(?:grand\s+total|total|amount\s+due)\b"
            r"|价税合计|实付|总金额|合计金额",
            text,
            re.I,
        ):
            numbers = re.findall(AMOUNT_PATTERN, text)
            if numbers:
                put("amount", money(cents(numbers[-1])), box, 0.98)
        if re.search(r"\b(?:tax|vat)\b|税额", text, re.I) and not re.search(
            r"税率|rate|invoice|发票", text, re.I
        ):
            numbers = re.findall(AMOUNT_PATTERN, text)
            if numbers:
                put("tax", money(cents(numbers[-1])), box)
        currency = re.search(r"\b(CNY|RMB|USD|EUR|GBP|JPY|HKD)\b|人民币|[¥￥€£$]", text, re.I)
        if currency:
            unit = currency.group(0).upper()
            unit = {
                "RMB": "CNY",
                "人民币": "CNY",
                "¥": "CNY",
                "￥": "CNY",
                "$": "USD",
                "€": "EUR",
                "£": "GBP",
            }.get(unit, unit)
            put("currency", unit, box, 0.95)
    if not fields["vendor"]["value"]:
        for box in boxes[:3]:
            if (
                not re.search(r"receipt|invoice|fictional|发票|虚构|sample", box["text"], re.I)
                and len(box["text"]) > 3
            ):
                put("vendor", box["text"], box, 0.55)
                break
    for qr in qrs:
        decoded = parse_qr(qr["payload"])
        for key, value in decoded.items():
            if key == "net_amount":
                # Standard VAT QR amount excludes tax. Compare only when OCR tax and total exist.
                if fields["tax"]["value"] and fields["amount"]["value"]:
                    expected = cents(value) + cents(fields["tax"]["value"])
                    if expected != cents(fields["amount"]["value"]):
                        warnings.append("QR net amount plus tax conflicts with the extracted total")
                        fields["amount"]["confidence"] = min(fields["amount"]["confidence"], 0.4)
                continue
            if key not in fields:
                continue
            old = fields[key]["value"]
            if old and old != value:
                warnings.append(f"QR {key} conflicts with extracted text: {value}")
                fields[key]["confidence"] = min(fields[key]["confidence"], 0.4)
            elif not old:
                put(key, value, {**qr, "text": qr["payload"], "confidence": 0.9}, 0.9)
    text = "\n".join(box["text"] for box in boxes).casefold()
    for category, words in (categories or DEFAULT_CATEGORIES).items():
        if any(word.casefold() in text for word in words):
            fields["category"].update(value=category, confidence=0.8)
            break
    if not fields["category"]["value"]:
        fields["category"].update(value="Other", confidence=0.4)
    for key in ["date", "amount", "vendor", "currency"]:
        if not fields[key]["value"]:
            warnings.append(f"Missing {key}; review required")
    return fields, warnings
