"""Configuration, errors and exact money parsing."""

import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


class ReceiptError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def success(data: object) -> dict:
    return {"ok": True, "data": data, "warnings": []}


def failure(exc: Exception) -> tuple[dict, int]:
    if isinstance(exc, ReceiptError):
        code, status = exc.code, exc.exit_code
    elif isinstance(exc, FileNotFoundError):
        code, status = "NOT_FOUND", 3
    elif isinstance(exc, (ValueError, TypeError, yaml.YAMLError)):
        code, status = "INVALID_DATA", 2
    else:
        code, status = "PROCESSING_ERROR", 1
    return {"ok": False, "error": {"code": code, "message": str(exc)}}, status


def cents(value: object) -> int | None:
    """Require finite amounts with at most two fractional places; never round silently."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ReceiptError("INVALID_AMOUNT", "Amount must be a decimal number")
    try:
        amount = Decimal(str(value).replace(",", "").strip())
        if not amount.is_finite() or abs(amount) > Decimal("9999999999.99"):
            raise InvalidOperation
        scaled = amount * 100
        if scaled != scaled.to_integral_value():
            raise InvalidOperation
        return int(scaled)
    except InvalidOperation:
        raise ReceiptError("INVALID_AMOUNT", f"Invalid two-decimal amount: {value}") from None


def money(value: int | None) -> str | None:
    return f"{Decimal(value) / 100:.2f}" if value is not None else None


def settings(
    data_dir: str | Path | None = None, config_path: str | None = None
) -> tuple[Path, dict]:
    root = Path(data_dir or os.environ.get("ROCR_DATA_DIR", "data")).expanduser().absolute()
    path = Path(config_path) if config_path else root / "config.yaml"
    defaults = {
        "timezone": "Asia/Shanghai",
        "low_confidence": 0.85,
        "max_file_mb": 50,
        "max_pages": 30,
        "max_pixels": 40000000,
        "tesseract_language": "chi_sim+eng",
        "ocr_timeout": 120,
        "categories_file": None,
    }
    if path.exists():
        config = yaml.safe_load(path.read_text("utf-8")) or {}
        if not isinstance(config, dict) or set(config) - set(defaults):
            raise ReceiptError("INVALID_CONFIG", "Config must use documented keys")
        defaults.update(config)
        if defaults["categories_file"]:
            defaults["categories_file"] = str((path.parent / defaults["categories_file"]).resolve())
    ZoneInfo(defaults["timezone"])
    for key in ["max_file_mb", "max_pages", "max_pixels", "ocr_timeout"]:
        if type(defaults[key]) is not int or defaults[key] <= 0:
            raise ReceiptError("INVALID_CONFIG", f"{key} must be a positive integer")
    if (
        not isinstance(defaults["low_confidence"], (int, float))
        or not 0 <= defaults["low_confidence"] <= 1
    ):
        raise ReceiptError("INVALID_CONFIG", "low_confidence must be between zero and one")
    return root, defaults


def now(config: dict) -> str:
    return datetime.now(ZoneInfo(config["timezone"])).isoformat(timespec="seconds")


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
