from __future__ import annotations

import httpx
import pytest

from shiftai_shared.m365.excel import ExcelConnector
from shiftai_shared.m365.graph_client import GraphClient, GraphError
from shiftai_shared.m365.onedrive import OneDriveConnector
from shiftai_shared.m365.word import DocSection, DocSpec, build_docx


class StaticTokens:
    def token(self) -> str:
        return "test-token"


def make_client(handler: httpx.MockTransport) -> GraphClient:
    return GraphClient(
        http=httpx.Client(transport=handler),
        token_provider=StaticTokens(),
        retries=3,
        sleep=lambda _: None,
    )


def test_retry_on_429_then_success() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="throttled")
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"value": 1})

    client = make_client(httpx.MockTransport(handler))
    assert client.get_json("/me") == {"value": 1}
    assert calls["n"] == 3


def test_4xx_is_permanent_graph_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(GraphError) as err:
        client.get_json("/drives/x")
    assert err.value.status_code == 404


def test_excel_reads_table_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/columns"):
            return httpx.Response(200, json={"value": [{"name": "colA"}, {"name": "colB"}]})
        return httpx.Response(
            200, json={"value": [{"values": [["a1", "b1"]]}, {"values": [["a2", "b2"]]}]}
        )

    excel = ExcelConnector(make_client(httpx.MockTransport(handler)))
    rows = excel.read_table_rows("d", "i", "T1")
    assert rows == [{"colA": "a1", "colB": "b1"}, {"colA": "a2", "colB": "b2"}]


def test_onedrive_upload_uses_fail_conflict_by_default() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "item-1", "webUrl": "https://x"})

    drive = OneDriveConnector(make_client(httpx.MockTransport(handler)))
    item = drive.upload_bytes("d", "parent", "brief.docx", b"bytes")
    assert item["id"] == "item-1"
    assert "conflictBehavior=fail" in seen["url"]


def test_docx_builder_deterministic_sections() -> None:
    spec = DocSpec(
        title="Title",
        subtitle="Sub",
        sections=(
            DocSection(
                heading="H1",
                paragraphs=("p1",),
                table_rows=(("k", "v"),),
                table_header=("Field", "Value"),
            ),
        ),
    )
    data = build_docx(spec)
    assert data[:2] == b"PK"  # valid zip container
    assert len(data) > 1000
