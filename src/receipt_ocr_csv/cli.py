"""Noninteractive CLI; progress goes to stderr and results are single JSON objects."""

import argparse
import json
import sys

from . import __version__
from .core.common import ReceiptError, failure, settings, success
from .core.store import Store


class Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise ReceiptError("INVALID_ARGUMENT", message)


def parser() -> Parser:
    root = Parser(prog="rocr", description="Offline receipt OCR and review")
    for key in ["data-dir", "config"]:
        root.add_argument("--" + key)
    for key in ["json", "quiet", "yes", "progress"]:
        root.add_argument("--" + key, action="store_true")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    scan = commands.add_parser("scan", help="Parse one receipt or a directory")
    scan.add_argument("path")
    scan.add_argument("--engine", choices=["rapidocr", "tesseract"], default="rapidocr")
    scan.add_argument("--recursive", action="store_true")
    scan.add_argument("--no-preprocess", action="store_true")
    listing = commands.add_parser("list")
    for key in ["status", "category", "date-from", "date-to"]:
        listing.add_argument("--" + key)
    listing.add_argument("--duplicates", action="store_true")
    show = commands.add_parser("show")
    show.add_argument("id")
    setting = commands.add_parser("set")
    setting.add_argument("id")
    setting.add_argument("fields", nargs="+", metavar="FIELD=VALUE")
    export = commands.add_parser("export")
    export.add_argument("--format", required=True, choices=["csv", "json", "xlsx"])
    export.add_argument("--out", required=True)
    export.add_argument("--no-bom", action="store_true")
    importing = commands.add_parser("import")
    importing.add_argument("path")
    backup = commands.add_parser("backup")
    backup.add_argument("--out")
    serving = commands.add_parser("serve")
    serving.add_argument("--host", default="127.0.0.1")
    serving.add_argument("--port", type=int, default=8889)
    commands.add_parser("mcp")
    return root


def globals_first(argv: list[str]) -> list[str]:
    front, rest = [], []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            rest.extend(argv[index:])
            break
        if arg in {"--json", "--quiet", "--yes", "--progress", "--version"}:
            front.append(arg)
        elif arg in {"--data-dir", "--config"}:
            if index + 1 >= len(argv):
                raise ReceiptError("INVALID_ARGUMENT", f"{arg} needs a value")
            front.extend(argv[index : index + 2])
            index += 1
        elif arg.startswith(("--data-dir=", "--config=")):
            front.append(arg)
        else:
            rest.append(arg)
        index += 1
    return front + rest


def main(argv: list[str] | None = None) -> int:
    store, args = None, None
    try:
        args = parser().parse_args(globals_first(list(sys.argv[1:] if argv is None else argv)))
        root, config = settings(args.data_dir, args.config)
        if args.command == "mcp":
            from .mcp_server import create_server

            create_server(root, config).run(transport="stdio")
            return 0
        if args.command == "serve":
            from .web.server import serve

            serve(root, config, args.host, args.port, args.quiet)
            return 0
        store = Store(root, config)
        command = args.command
        if command == "scan":
            progress = (
                (lambda msg: print(msg, file=sys.stderr))
                if args.progress and not args.quiet
                else None
            )
            data = store.scan(
                args.path, args.engine, args.recursive, not args.no_preprocess, progress
            )
        elif command == "list":
            data = store.list(
                {
                    key: getattr(args, key)
                    for key in ["status", "category", "date_from", "date_to", "duplicates"]
                }
            )
        elif command == "show":
            data = store.show(args.id)
        elif command == "set":
            values = {}
            for assignment in args.fields:
                if "=" not in assignment:
                    raise ReceiptError("INVALID_ARGUMENT", "Use FIELD=VALUE assignments")
                key, value = assignment.split("=", 1)
                if key in values:
                    raise ReceiptError("INVALID_ARGUMENT", f"Repeated field: {key}")
                values[key] = value
            data = store.update(args.id, values)
        elif command == "export":
            data = store.export(args.format, args.out, args.yes, not args.no_bom)
        elif command == "import":
            data = store.import_file(args.path)
        elif command == "backup":
            data = store.backup(args.out)
        result, status = success(data), 0
        if command == "scan" and data["errors"]:
            result = {
                "ok": False,
                "error": {"code": "SCAN_FAILED", "message": "One or more files failed"},
                "data": data,
            }
            status = 1
    except Exception as exc:
        result, status = failure(exc)
    finally:
        if store:
            store.close()
    print(json.dumps(result, ensure_ascii=False, indent=None if args and args.json else 2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
