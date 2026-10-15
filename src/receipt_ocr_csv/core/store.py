"""SQLite records, source provenance, review, interchange and complete backups."""

import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from datetime import date
from pathlib import Path

import yaml

from .common import ReceiptError, cents, json_text, money, now
from .extraction import DEFAULT_CATEGORIES, NAMES, extract_fields
from .imaging import decode_document

EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".pdf"}
COLUMNS = [
    "id",
    "date",
    "amount",
    "tax",
    "currency",
    "vendor",
    "buyer",
    "invoice_no",
    "file",
    "invoice_code",
    "check_code",
    "payment_method",
    "category",
    "note",
    "status",
    "duplicate_of",
]


class Store:
    def __init__(self, root: Path, config: dict):
        self.root, self.config = root, config
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "receipts.db", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS documents(
          id TEXT PRIMARY KEY,path TEXT NOT NULL,sha1 TEXT UNIQUE,pages INTEGER NOT NULL,
          engine TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,
          duplicate_of TEXT,identity_key TEXT,details_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fields(doc_id TEXT NOT NULL REFERENCES documents(id),
          name TEXT NOT NULL,value TEXT NOT NULL,confidence REAL NOT NULL,bbox_json TEXT NOT NULL,
          edited INTEGER NOT NULL,PRIMARY KEY(doc_id,name));
        CREATE TABLE IF NOT EXISTS records(doc_id TEXT PRIMARY KEY REFERENCES documents(id),
          date TEXT,amount_cents INTEGER,tax_cents INTEGER,currency TEXT,vendor TEXT,buyer TEXT,
          invoice_no TEXT,category TEXT,note TEXT);
        """)
        self.categories = DEFAULT_CATEGORIES
        category_path = config.get("categories_file") or root / "categories.yaml"
        if Path(category_path).exists():
            self.categories = yaml.safe_load(Path(category_path).read_text("utf-8"))
            if not isinstance(self.categories, dict) or not all(
                isinstance(k, str)
                and isinstance(v, list)
                and all(isinstance(word, str) and word for word in v)
                for k, v in self.categories.items()
            ):
                raise ReceiptError(
                    "INVALID_CATEGORIES", "Categories must map names to keyword lists"
                )

    def close(self):
        self.db.close()

    def show(self, identifier: str) -> dict:
        row = self.db.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ReceiptError("NOT_FOUND", f"Document not found: {identifier}", 3)
        result = dict(row)
        result.pop("identity_key")
        result["details"] = json.loads(result.pop("details_json"))
        fields = {}
        for field in self.db.execute("SELECT * FROM fields WHERE doc_id=?", (identifier,)):
            fields[field["name"]] = {
                "value": field["value"],
                "confidence": field["confidence"],
                "sources": json.loads(field["bbox_json"]),
                "edited": bool(field["edited"]),
                "low_confidence": field["confidence"] < self.config["low_confidence"],
            }
        result["fields"] = fields
        result["record"] = {key: fields[key]["value"] for key in NAMES}
        return result

    def list(self, filters: dict | None = None) -> list[dict]:
        filters = filters or {}
        if not isinstance(filters, dict):
            raise ReceiptError("INVALID_FILTER", "Filters must be an object")
        if "duplicates" in filters and not isinstance(filters["duplicates"], bool):
            raise ReceiptError("INVALID_FILTER", "duplicates must be boolean")
        if set(filters) - {"status", "category", "date_from", "date_to", "duplicates"}:
            raise ReceiptError("INVALID_FILTER", "Unknown record filter")
        if filters.get("status") and filters["status"] not in {"new", "parsed", "reviewed"}:
            raise ReceiptError("INVALID_STATUS", "Status must be new, parsed or reviewed")
        for key in ["date_from", "date_to"]:
            if filters.get(key):
                date.fromisoformat(filters[key])
        result = []
        for row in self.db.execute("SELECT id FROM documents ORDER BY created_at,rowid"):
            item = self.show(row["id"])
            record = item["record"]
            if filters.get("status") and item["status"] != filters["status"]:
                continue
            if filters.get("category") and record["category"] != filters["category"]:
                continue
            if filters.get("date_from") and record["date"] < filters["date_from"]:
                continue
            if filters.get("date_to") and record["date"] > filters["date_to"]:
                continue
            if filters.get("duplicates") and not item["duplicate_of"]:
                continue
            result.append(
                {
                    "id": item["id"],
                    **record,
                    "status": item["status"],
                    "duplicate_of": item["duplicate_of"],
                    "engine": item["engine"],
                    "file": str(next((self.root / "files" / item["id"]).glob("source.*"), "")),
                }
            )
        return result

    def _save_fields(self, identifier: str, fields: dict):
        for name, field in fields.items():
            self.db.execute(
                "INSERT OR REPLACE INTO fields VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    name,
                    str(field["value"]),
                    field["confidence"],
                    json_text(field["sources"]),
                    int(field["edited"]),
                ),
            )
        values = {name: field["value"] for name, field in fields.items()}
        self.db.execute(
            "INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                values["date"],
                cents(values["amount"]),
                cents(values["tax"]),
                values["currency"],
                values["vendor"],
                values["buyer"],
                values["invoice_no"],
                values["category"],
                values["note"],
            ),
        )
        if values["invoice_no"]:
            identity = "invoice:" + values["invoice_code"] + ":" + values["invoice_no"]
        elif all(values[key] for key in ["date", "amount", "vendor"]):
            identity = "fallback:" + json_text(
                [values["date"], values["amount"], values["vendor"].casefold().strip()]
            )
        else:
            identity = None
        self.db.execute("UPDATE documents SET identity_key=? WHERE id=?", (identity, identifier))

    def _deduplicate(self):
        canonical = {}
        for row in self.db.execute(
            "SELECT id,identity_key FROM documents ORDER BY created_at,rowid"
        ).fetchall():
            key = row["identity_key"]
            duplicate = canonical.get(key) if key else None
            self.db.execute(
                "UPDATE documents SET duplicate_of=? WHERE id=?", (duplicate, row["id"])
            )
            if key:
                canonical.setdefault(key, row["id"])

    def scan(
        self,
        source: str,
        engine: str = "rapidocr",
        recursive: bool = False,
        use_preprocess: bool = True,
        progress=None,
    ) -> dict:
        path = Path(source).expanduser().absolute()
        if not path.exists():
            raise ReceiptError("NOT_FOUND", f"Input does not exist: {path}", 3)
        if path.is_symlink():
            raise ReceiptError("UNSAFE_PATH", "Symlink inputs are not supported")
        candidates = (
            sorted(path.rglob("*") if recursive else path.iterdir()) if path.is_dir() else [path]
        )
        candidates = [p for p in candidates if p.suffix.lower() in EXTENSIONS and not p.is_dir()]
        if not candidates:
            raise ReceiptError("NO_INPUT", "No supported receipt files found")
        processed, errors = [], []
        for index, candidate in enumerate(candidates, 1):
            if progress:
                progress(f"Processing {index}/{len(candidates)}: {candidate.name}")
            try:
                processed.append(self._scan_file(candidate, engine, use_preprocess))
            except Exception as exc:
                from .common import failure

                errors.append({"path": str(candidate), **failure(exc)[0]["error"]})
        return {"processed": processed, "errors": errors, "count": len(processed)}

    def _scan_file(self, path: Path, engine: str, use_preprocess: bool) -> dict:
        if any(part.is_symlink() for part in [path, *path.parents]):
            raise ReceiptError(
                "UNSAFE_PATH", "Symlink inputs or parent directories are not supported"
            )
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ReceiptError("UNSAFE_PATH", "Input must be a regular file")
            if info.st_size > self.config["max_file_mb"] * 1024 * 1024:
                raise ReceiptError("FILE_TOO_LARGE", "Input exceeds configured file size")
            content = source.read(self.config["max_file_mb"] * 1024 * 1024 + 1)
        if len(content) > self.config["max_file_mb"] * 1024 * 1024:
            raise ReceiptError("FILE_TOO_LARGE", "Input exceeds configured file size")
        digest = hashlib.sha1(content).hexdigest()
        existing = self.db.execute("SELECT id FROM documents WHERE sha1=?", (digest,)).fetchone()
        if existing:
            return {
                "id": existing["id"],
                "reused": True,
                "duplicate_of": self.show(existing["id"])["duplicate_of"],
            }
        identifier = uuid.uuid4().hex
        directory = self.root / "files" / identifier
        directory.mkdir(parents=True)
        saved = directory / ("source" + path.suffix.lower())
        saved.write_bytes(content)
        try:
            details = decode_document(saved, directory, engine, self.config, use_preprocess)
            fields, warnings = extract_fields(details["boxes"], details["qr"], self.categories)
            details["warnings"] = warnings
            methods = sorted({page["engine"] for page in details["pages"]})
            with self.db:
                self.db.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        str(path),
                        digest,
                        len(details["pages"]),
                        "+".join(methods),
                        "parsed",
                        now(self.config),
                        None,
                        None,
                        json_text(details),
                    ),
                )
                self._save_fields(identifier, fields)
                self._deduplicate()
        except Exception:
            shutil.rmtree(directory)
            raise
        return {
            "id": identifier,
            "reused": False,
            "duplicate_of": self.show(identifier)["duplicate_of"],
            "warnings": warnings,
        }

    def update(self, identifier: str, values: dict) -> dict:
        normalized = self.validate(values)
        item = self.show(identifier)
        for key, value in normalized.items():
            item["fields"][key].update(value=value, confidence=1.0, edited=True)
        with self.db:
            self._save_fields(identifier, item["fields"])
            self.db.execute("UPDATE documents SET status='reviewed' WHERE id=?", (identifier,))
            self._deduplicate()
        return self.show(identifier)

    @staticmethod
    def validate(values: dict) -> dict:
        if not isinstance(values, dict) or set(values) - set(NAMES):
            raise ReceiptError("INVALID_FIELD", "Only documented receipt fields may be changed")
        result = {}
        for key, value in values.items():
            if value is None:
                value = ""
            if (
                not isinstance(value, (str, int, float))
                or isinstance(value, bool)
                or len(str(value)) > 10000
            ):
                raise ReceiptError("INVALID_FIELD", f"Invalid {key}")
            text = str(value).strip()
            if key in {"amount", "tax"}:
                text = money(cents(text)) or ""
            if key == "date" and text:
                if date.fromisoformat(text).isoformat() != text:
                    raise ReceiptError("INVALID_DATE", "Use ISO YYYY-MM-DD dates")
            if key == "currency" and text:
                if len(text) != 3 or not text.isascii() or not text.isalpha():
                    raise ReceiptError("INVALID_CURRENCY", "Use a three-letter currency code")
                text = text.upper()
            result[key] = text
        return result

    def export(
        self, format: str, destination: str, overwrite: bool = False, bom: bool = True
    ) -> dict:
        if format not in {"csv", "json", "xlsx"}:
            raise ReceiptError("INVALID_FORMAT", "Format must be csv, json or xlsx")
        out = Path(destination).expanduser().absolute()
        if out.exists() and not overwrite:
            raise ReceiptError("EXISTS", "Output exists; use --yes to replace it")
        rows = self.list()
        clean = [{key: row.get(key) or "" for key in COLUMNS} for row in rows]
        buffer = io.BytesIO()
        if format == "json":
            buffer.write(
                json.dumps(
                    {"schema_version": 1, "records": clean}, ensure_ascii=False, indent=2
                ).encode()
            )
        elif format == "csv":
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(clean)
            buffer.write(stream.getvalue().encode("utf-8-sig" if bom else "utf-8"))
        else:
            from openpyxl import Workbook

            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Receipts"
            sheet.append(COLUMNS)
            for row in clean:
                sheet.append([row[key] for key in COLUMNS])
            # Keep user/vendor text literal, never spreadsheet formulas.
            for row in sheet:
                for cell in row:
                    cell.data_type = "s"
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            workbook.save(buffer)
        out.parent.mkdir(parents=True, exist_ok=True)
        mode = "wb" if overwrite else "xb"
        with out.open(mode) as file:
            file.write(buffer.getvalue())
        return {"path": str(out), "format": format, "count": len(rows)}

    def import_file(self, source: str) -> dict:
        path = Path(source)
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ReceiptError("FILE_TOO_LARGE", "Import exceeds 20 MiB")
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text("utf-8-sig"))
            if isinstance(payload, dict) and payload.get("schema_version", 1) != 1:
                raise ReceiptError("INVALID_IMPORT", "Unsupported schema version")
            rows = payload.get("records") if isinstance(payload, dict) else payload
        elif path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
        else:
            raise ReceiptError("INVALID_FORMAT", "Import requires JSON or CSV")
        if not isinstance(rows, list) or len(rows) > 100000:
            raise ReceiptError("INVALID_IMPORT", "Expected at most 100000 record objects")
        normalized = []
        for row in rows:
            if not isinstance(row, dict) or set(row) - set(COLUMNS):
                raise ReceiptError("INVALID_IMPORT", "Record contains unknown columns")
            normalized.append(self.validate({key: row.get(key, "") for key in NAMES}))
        inserted, reused = [], 0
        with self.db:
            for values in normalized:
                digest = hashlib.sha1(("import:" + json_text(values)).encode()).hexdigest()
                existing = self.db.execute(
                    "SELECT id FROM documents WHERE sha1=?", (digest,)
                ).fetchone()
                if existing:
                    reused += 1
                    continue
                identifier = uuid.uuid4().hex
                self.db.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        "import:" + path.name,
                        digest,
                        0,
                        "import",
                        "reviewed",
                        now(self.config),
                        None,
                        None,
                        json_text(
                            {
                                "boxes": [],
                                "pages": [],
                                "qr": [],
                                "warnings": ["Imported values have no source image"],
                            }
                        ),
                    ),
                )
                fields = {
                    key: {"value": value, "confidence": 1.0, "sources": [], "edited": True}
                    for key, value in values.items()
                }
                self._save_fields(identifier, fields)
                inserted.append(identifier)
            self._deduplicate()
        return {"imported": len(inserted), "reused": reused, "ids": inserted}

    def backup(self, destination: str | None = None) -> dict:
        stamp = now(self.config).replace(":", "").replace("+", "_")
        out = (
            Path(destination)
            if destination
            else self.root / "backups" / f"receipts-{stamp}-{uuid.uuid4().hex[:6]}.zip"
        )
        if out.exists():
            raise ReceiptError("EXISTS", "Backup destination already exists")
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "receipts.db"
            with sqlite3.connect(snapshot) as target:
                self.db.backup(target)
            with zipfile.ZipFile(out, "x", zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, "receipts.db")
                archive.writestr("effective-config.json", json_text(self.config))
                archive.writestr(
                    "categories.yaml", yaml.safe_dump(self.categories, allow_unicode=True)
                )
                for path in (self.root / "files").rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        archive.write(path, str(path.relative_to(self.root)))
                if (self.root / "config.yaml").is_file():
                    archive.write(self.root / "config.yaml", "config.yaml")
        return {"path": str(out.absolute()), "count": len(self.list())}

    def preview(self, identifier: str, page: int) -> Path:
        item = self.show(identifier)
        if page < 1 or page > item["pages"]:
            raise ReceiptError("NOT_FOUND", "Preview page does not exist", 3)
        return self.root / "files" / identifier / f"page-{page}.png"
