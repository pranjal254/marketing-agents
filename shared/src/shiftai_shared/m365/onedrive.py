"""OneDrive-over-Graph: watched-folder listing, uploads, folder creation.

Uploads use conflict behavior "fail" by default so an idempotent retry never
silently overwrites an existing artifact.
"""

from __future__ import annotations

from typing import Any

from shiftai_shared.m365.graph_client import GraphClient


class OneDriveConnector:
    def __init__(self, graph: GraphClient) -> None:
        self._graph = graph

    def list_children(self, drive_id: str, folder_id: str) -> list[dict[str, Any]]:
        data = self._graph.get_json(f"/drives/{drive_id}/items/{folder_id}/children")
        return list(data.get("value", []))

    def download_bytes(self, drive_id: str, item_id: str) -> bytes:
        response = self._graph.request("GET", f"/drives/{drive_id}/items/{item_id}/content")
        return response.content

    def upload_bytes(
        self,
        drive_id: str,
        parent_id: str,
        name: str,
        content: bytes,
        *,
        content_type: str = "application/octet-stream",
        conflict_behavior: str = "fail",
    ) -> dict[str, Any]:
        path = (
            f"/drives/{drive_id}/items/{parent_id}:/{name}:/content"
            f"?@microsoft.graph.conflictBehavior={conflict_behavior}"
        )
        return self._graph.put_content(path, content, content_type)

    def ensure_folder(self, drive_id: str, parent_id: str, name: str) -> dict[str, Any]:
        return self._graph.post_json(
            f"/drives/{drive_id}/items/{parent_id}/children",
            {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "replace"},
        )
