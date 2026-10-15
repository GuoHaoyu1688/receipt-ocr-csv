import asyncio
import json
import os
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from receipt_ocr_csv.cli import main
from receipt_ocr_csv.core.common import ReceiptError, settings
from receipt_ocr_csv.mcp_server import create_server
from receipt_ocr_csv.web.server import make_server


def test_cli_contract(tmp_path, text_pdf, capsys):
    common = ["--data-dir", str(tmp_path / "cli"), "--json"]
    assert main(["scan", str(text_pdf), *common]) == 0
    identifier = json.loads(capsys.readouterr().out)["data"]["processed"][0]["id"]
    for args in [
        ["list"],
        ["show", identifier],
        ["set", identifier, "amount=128.50"],
        ["export", "--format", "csv", "--out", str(tmp_path / "cli.csv")],
        ["backup"],
        ["import", str(tmp_path / "cli.csv")],
    ]:
        assert main([*args, *common]) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
    for args, status, code in [
        (["show", "missing"], 3, "NOT_FOUND"),
        (["list", "--bad"], 2, "INVALID_ARGUMENT"),
        (["set", identifier, "amount=oops"], 2, "INVALID_AMOUNT"),
    ]:
        assert main([*args, *common]) == status
        assert json.loads(capsys.readouterr().out)["error"]["code"] == code
    assert main(["list", "--data-dir"]) == 2
    json.loads(capsys.readouterr().out)


def test_mcp_all_tools(tmp_path, text_pdf):
    root, config = settings(tmp_path / "mcp")
    server = create_server(root, config)

    async def run():
        tools = await server.list_tools()
        assert {tool.name for tool in tools} == {
            "ocr_receipt",
            "list_records",
            "update_record",
            "export_records",
        }
        first = await server.call_tool("ocr_receipt", {"path": str(text_pdf)})
        data = json.loads(first[0].text)["data"]
        identifier = data["processed"][0]["id"]
        listing = await server.call_tool("list_records", {"filters": {"status": "parsed"}})
        assert len(json.loads(listing[0].text)["data"]) == 1
        updated = await server.call_tool(
            "update_record", {"id": identifier, "fields": {"amount": "128.50"}}
        )
        assert json.loads(updated[0].text)["data"]["record"]["amount"] == "128.50"
        exported = await server.call_tool(
            "export_records", {"format": "json", "path": str(tmp_path / "mcp.json")}
        )
        assert json.loads(exported[0].text)["ok"]
        failed = await server.call_tool("update_record", {"id": "missing", "fields": {}})
        assert not json.loads(failed[0].text)["ok"]

    asyncio.run(run())


def test_actual_mcp_stdio(tmp_path):
    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "receipt_ocr_csv.cli", "--data-dir", str(tmp_path / "stdio"), "mcp"],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 4
            result = await session.call_tool("list_records", {})
            assert json.loads(result.content[0].text)["data"] == []

    asyncio.run(run())


def test_http_review_and_origin(store, text_pdf):
    identifier = store.scan(str(text_pdf))["processed"][0]["id"]
    server = make_server(store.root, store.config, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert b"A clearer view" in urlopen(base + "/").read()
        assert urlopen(base + "/favicon.ico").status == 204
        records = json.load(urlopen(base + "/api/records"))
        assert len(records["data"]) == 1
        assert json.load(urlopen(base + "/api/document?id=" + identifier))["ok"]
        assert (
            urlopen(base + "/api/preview?id=" + identifier + "&page=1")
            .read()
            .startswith(b"\x89PNG")
        )
        body = json.dumps({"id": identifier, "fields": {"amount": "99.90"}}).encode()
        request = Request(
            base + "/api/update",
            data=body,
            headers={"Content-Type": "application/json", "Origin": base},
        )
        assert json.load(urlopen(request))["data"]["record"]["amount"] == "99.90"
        hostile = Request(
            base + "/api/update",
            data=body,
            headers={"Content-Type": "application/json", "Origin": "http://evil.invalid"},
        )
        with pytest.raises(HTTPError):
            urlopen(hostile)
        with pytest.raises(HTTPError):
            urlopen(base + "/api/preview?id=../../etc/passwd")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    with pytest.raises(ReceiptError):
        make_server(store.root, store.config, "0.0.0.0", 0)


def test_cli_subprocess(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "receipt_ocr_csv.cli",
            "list",
            "--json",
            "--data-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"ok": True, "data": [], "warnings": []}
