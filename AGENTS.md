# Agent operating manual

Use this repository to turn local receipt images/PDFs into auditable, reviewed expense records. The CLI works on a headless Linux machine; the Web UI is optional.

## Install from zero

```bash
git clone https://github.com/GuoHaoyu1688/receipt-ocr-csv.git
cd receipt-ocr-csv
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
export ROCR_DATA_DIR=/workspace/data/receipt-ocr-csv
```

Python 3.11+ on Linux or macOS is required. On minimal Linux: `sudo apt-get install libgl1 libglib2.0-0`. RapidOCR and its local Chinese/English weights are installed by pip. HEIC: `pip install -e '.[heic]'`. Tesseract: `sudo apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim`. Select it with `--engine tesseract`. Developer tools: `pip install -e '.[dev]'`. A macOS environment that marks editable `.pth` files hidden may require `chflags nohidden .venv/lib/python3.12/site-packages/*.pth` in that venv only.

## Paths and configuration

Default root: `./data`. Precedence: `--data-dir` > `ROCR_DATA_DIR` > default. Database: `<root>/receipts.db`. Originals and processed page PNGs: `<root>/files/<id>/`. Default backup: `<root>/backups/receipts-<timestamp>-<suffix>.zip`.

Optional config: `<root>/config.yaml`, overridden by `--config PATH`. Supported keys: timezone (`Asia/Shanghai`), low_confidence (0.85), max_file_mb (50), max_pages (30), max_pixels (40000000), tesseract_language (`chi_sim+eng`), ocr_timeout (120 seconds, Tesseract only), categories_file (null). Relative category-file paths are resolved relative to the config. Otherwise `<root>/categories.yaml` supplies ordered category names mapped to keyword lists. First keyword match wins. Preserve original files and back up before edits or moving the library.

## Commands and real output shapes

Every command accepts global `--json`, `--quiet`, `--data-dir`, `--config`, `--progress`, `--yes` and `--version` where meaningful; global flags may appear after the subcommand. No TTY or prompts are required.

| Command | Parameters and example |
| --- | --- |
| scan | `rocr scan examples/receipts --recursive --engine rapidocr --json --progress`; optional `--no-preprocess` |
| list | `rocr list --status parsed --category Meals --date-from 2026-01-01 --date-to 2026-01-31 --json`; optional `--duplicates` |
| show | `rocr show ID --json` |
| set | `rocr set ID amount=128.50 category=Transport 'note=Reviewed locally' --json` |
| export | `rocr export --format csv --out receipts.csv --json`; formats csv/json/xlsx; optional `--no-bom`, `--yes` |
| import | `rocr import receipts.csv --json`; JSON also supported |
| backup | `rocr backup --json`; optional `--out PATH.zip` |
| serve | `rocr serve --host 127.0.0.1 --port 8889`; stderr startup URL, blocks until interrupted |
| mcp | `rocr mcp`; stdin/stdout reserved for MCP protocol |

Run this on a fresh root to reproduce the output:

```bash
rocr list --data-dir data/empty-example --json
```

```json
{"ok":true,"data":[],"warnings":[]}
```

`rocr show missing --data-dir data/empty-example --json` exits 3 with:

```json
{"ok":false,"error":{"code":"NOT_FOUND","message":"Document not found: missing"}}
```

Scan returns `data.processed` (id, reused, duplicate_of, warnings), `data.errors` and count. IDs are random 32-character hex strings. Read them from output rather than guessing. `show` returns document metadata, `record`, `fields` and `details` (pages, text boxes, QR payloads, original extraction warnings). Each field exposes value, confidence, edited, sources and low_confidence. `list` returns flat rows with engine/status/duplicate_of. `set` returns full updated document. `export` returns path/format/count. `import` returns imported/reused/ids. `backup` returns path/count.

Exit codes are 0 success; 1 processing failure or partial batch failure; 2 argument/validation failure; 3 missing data. `--progress` goes only to stderr. JSON errors use error.code and error.message. A failed batch may still have successful documents: inspect `data.processed` before retrying.

## Idempotency and review semantics

Identical source bytes reuse their ID and preserve corrections. Changing source bytes adds a document; logical duplicates are flagged using invoice code/number, or date + amount + normalized vendor if no number exists. `duplicate_of` points to the earliest existing canonical row. Edits recalculate duplicate relationships.

`set` accepts date, amount, tax, currency, vendor, buyer, invoice_no, invoice_code, check_code, payment_method, category and note. An empty value clears a field. Amount and tax use two-decimal strings and integer cents internally; do not convert money through binary floating-point. Dates must be ISO YYYY-MM-DD. Saving marks the record reviewed; original source coordinates/text and extraction warnings are retained. Confidence after a manual edit is 1.0 as a review marker, not an OCR accuracy claim.

Import validates all rows before committing. It creates reviewed records without images and ignores incoming identity/status/duplicate pointers as update directives. Unchanged normalized imports are idempotent; edits to an imported row do not change its ingestion identity. JSON imports accept `{schema_version:1, records:[...]}` or a bare array. CSV requires documented columns. Unknown columns fail. Exported IDs are for traceability, not authority.

## MCP integration

```json
{
  "mcpServers": {
    "receipt_ocr": {
      "command": "/workspace/apps/receipt-ocr-csv/.venv/bin/rocr",
      "args": ["--data-dir", "/workspace/data/receipt-ocr-csv", "mcp"]
    }
  }
}
```

Alternative: `python -m receipt_ocr_csv.mcp_server` with `ROCR_DATA_DIR` set. Official FastMCP stdio tools share the core and return JSON envelopes. Exact SDK schemas: [docs/mcp-tools.json](docs/mcp-tools.json).

| Tool | Parameters | Side effects |
| --- | --- | --- |
| ocr_receipt | path: required string; engine: string rapidocr/tesseract, default rapidocr; recursive: boolean false; preprocess: boolean true | Copies originals/previews and writes parsed records |
| list_records | filters: optional object with status, category, ISO date_from/date_to, boolean duplicates | None |
| update_record | id: required string; fields: required object with allowed field values | Saves reviewed corrections and duplicate flags |
| export_records | format: required string csv/json/xlsx; path: required string | Creates a new file; existing paths fail |

No delete tool exists. Never use review confidence or QR content as invoice-authenticity evidence.

## Task recipes

1. **“Process this receipt folder.”** Run `rocr scan /path/to/receipts --recursive --json --progress`; inspect partial errors. Then `rocr list --status parsed --json`. Review missing date/amount/vendor/currency and low-confidence fields through `show`. Do not silently change ambiguous values.
2. **“Correct this total.”** Run `rocr backup --json`, then `rocr set ID amount=128.50 --json`; verify `rocr show ID --json`. Repeat scans preserve the correction.
3. **“Prepare expense import.”** Run `rocr list --duplicates --json`; preserve duplicate warnings for the user. Export with `rocr export --format csv --out /path/to/receipts.csv --json`. Stable columns are listed in README. Do not sum duplicate receipts blindly.
4. **“Move to another machine.”** Run `rocr backup --out /path/to/receipts-backup.zip --json`. Stop writers on the destination, extract a trusted archive into an empty root, restore external config if used, and run `rocr list --data-dir NEW_ROOT --json`.
5. **“Read a damaged phone photo.”** Scan normally first and inspect `show`. If preprocessing worsens it, copy/transform the input under a new filename with changed bytes and scan with `--no-preprocess` or use Tesseract. There is no destructive reprocess command in this version.

## Safety and operational limits

Only scan user-selected local paths. No network OCR or authenticity checks are made. Input symlinks are rejected. Size/page/pixel limits are configurable but this is not a sandbox for hostile documents. Large OCR jobs take longer than two seconds; batch mode reuses the local engine. Optional Tesseract has a timeout; RapidOCR runs in-process without a hard timeout.

The UI refuses non-loopback binds and checks Host/Origin for writes. Do not proxy it publicly. There is no authentication or encryption. User data is outside version control; never commit receipts, runtime databases or backups. Export is exclusive by default; `--yes` can overwrite an explicit export path. Backups never overwrite. Before changing fields, config, source copies or the database, make a backup. Do not modify database schema or private files directly during routine work.

One PDF is one record, even if it has several pages; split unrelated invoices first. Text-layer detection uses a 20-character threshold per page. Chinese labeled invoice rules do not cover every dense layout; severe blur, perspective and handwriting require manual review. All screenshots/fixtures must remain fictional.

## Test and build

```bash
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest -q --cov=receipt_ocr_csv
```

`pytest -q -m 'not ocr and not system'` is a fast subset, not full acceptance. Real ONNX acceptance is required for release. CI includes it on Python 3.11/3.12 and excludes optional system Tesseract. Regenerate examples with `python examples/generate_receipts.py`; validate against `examples/ground-truth.json`. Do not replace OCR inference with saved text when reporting accuracy. `uv build` builds source and wheel distributions.
