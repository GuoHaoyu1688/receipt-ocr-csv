"""Official SDK stdio tools sharing the CLI core."""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .core.common import failure, settings, success
from .core.store import Store


def create_server(root: Path, config: dict) -> FastMCP:
    server = FastMCP("receipt-ocr-csv")

    def invoke(method, *args):
        store = None
        try:
            store = Store(root, config)
            result = getattr(store, method)(*args)
            if method == "scan" and result["errors"]:
                return {
                    "ok": False,
                    "error": {"code": "SCAN_FAILED", "message": "Receipt scan failed"},
                    "data": result,
                }
            return success(result)
        except Exception as exc:
            return failure(exc)[0]
        finally:
            if store:
                store.close()

    @server.tool()
    def ocr_receipt(
        path: str, engine: str = "rapidocr", recursive: bool = False, preprocess: bool = True
    ) -> dict:
        """OCR local input; save originals, previews and records. Hashes deduplicate reruns."""
        return invoke("scan", path, engine, recursive, preprocess)

    @server.tool()
    def list_records(filters: dict | None = None) -> dict:
        """Read records filtered by status, category, ISO date_from/date_to and duplicates."""
        return invoke("list", filters)

    @server.tool()
    def update_record(id: str, fields: dict) -> dict:
        """Write reviewed corrections and duplicate flags. Use decimal amount/tax and ISO date."""
        return invoke("update", id, fields)

    @server.tool()
    def export_records(format: str, path: str) -> dict:
        """Write CSV, JSON or XLSX to a new local path. Existing files are never overwritten."""
        return invoke("export", format, path)

    return server


def main():
    root, config = settings()
    create_server(root, config).run(transport="stdio")


if __name__ == "__main__":
    main()
