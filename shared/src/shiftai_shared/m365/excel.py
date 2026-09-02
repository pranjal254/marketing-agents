"""Excel-over-Graph reads: workbook tables (intake responses, plan sheets, trackers).

Microsoft Forms responses are read from the Forms-linked Excel responses workbook —
there is no stable public Graph API for Forms responses (recorded PLAN.md Q3 decision).
"""

from __future__ import annotations

from typing import Any

from shiftai_shared.m365.graph_client import GraphClient


class ExcelConnector:
    def __init__(self, graph: GraphClient) -> None:
        self._graph = graph

    def read_table_rows(self, drive_id: str, item_id: str, table_name: str) -> list[dict[str, Any]]:
        """Return table rows as dicts keyed by the table's column headers."""
        base = f"/drives/{drive_id}/items/{item_id}/workbook/tables/{table_name}"
        columns = self._graph.get_json(f"{base}/columns?$select=name")
        headers = [c["name"] for c in columns.get("value", [])]
        body = self._graph.get_json(f"{base}/rows")
        rows: list[dict[str, Any]] = []
        for row in body.get("value", []):
            values = row.get("values", [[]])[0]
            rows.append(dict(zip(headers, values, strict=False)))
        return rows

    def append_table_row(
        self, drive_id: str, item_id: str, table_name: str, values: list[Any]
    ) -> dict[str, Any]:
        base = f"/drives/{drive_id}/items/{item_id}/workbook/tables/{table_name}"
        return self._graph.post_json(f"{base}/rows", {"values": [values]})
