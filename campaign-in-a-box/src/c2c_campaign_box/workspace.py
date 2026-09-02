"""Campaign workspace binding (step 7) — versioned folder/naming template only.

OneDrive in production, a local folder in dev/tests, behind one protocol. Writes are
additive (new files only, conflict = fail); nothing here deletes, moves or
overwrites — guardrail 3 is structural: no such method exists on the protocol.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from c2c_campaign_box.agent_config import OrchestratorConfig


class WorkspaceWriteError(Exception):
    """A workspace write failed (upload conflict, IO failure) — escalates to AiCoE."""


@dataclass(frozen=True)
class WorkspaceFile:
    name: str
    ref: str
    modified_at: str | None = None


class CampaignWorkspace(Protocol):
    """Additive-only workspace surface. No delete / move / overwrite exists."""

    def ensure_folder(self, path: str) -> str: ...

    def upload(self, folder_path: str, filename: str, content: bytes) -> str:
        """New file only — an existing file with the same name is an error."""
        ...

    def download(self, ref: str) -> bytes: ...

    def list_files(self, folder_path: str) -> list[WorkspaceFile]: ...


class LocalCampaignWorkspace:
    """Dev/test binding: a local folder tree. Refs are absolute paths."""

    def __init__(self, root: str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> str:
        return str(self._root)

    def ensure_folder(self, path: str) -> str:
        target = self._root / path
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    def upload(self, folder_path: str, filename: str, content: bytes) -> str:
        folder = self._root / folder_path
        target = folder / filename
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise WorkspaceWriteError(f"refusing to overwrite existing file: {target}")
            target.write_bytes(content)
        except OSError as exc:  # includes Windows MAX_PATH failures — typed, never raw
            raise WorkspaceWriteError(f"workspace write failed for {target}: {exc}") from exc
        return str(target)

    def download(self, ref: str) -> bytes:
        return Path(ref).read_bytes()

    def list_files(self, folder_path: str) -> list[WorkspaceFile]:
        folder = self._root / folder_path
        if not folder.is_dir():
            return []
        out: list[WorkspaceFile] = []
        for p in sorted(folder.iterdir()):
            if p.is_file():
                out.append(WorkspaceFile(name=p.name, ref=str(p)))
        return out


class OneDriveCampaignWorkspace:
    """Production binding over the shared OneDrive connector (uploads use
    conflictBehavior=fail — overwrites are impossible)."""

    def __init__(self, connector: Any, drive_id: str, root_folder_id: str) -> None:
        self._connector = connector
        self._drive_id = drive_id
        self._root_folder_id = root_folder_id
        self._folder_ids: dict[str, str] = {"": root_folder_id}

    def ensure_folder(self, path: str) -> str:
        parent_id = self._root_folder_id
        walked = ""
        for part in [p for p in path.split("/") if p]:
            walked = f"{walked}/{part}" if walked else part
            if walked not in self._folder_ids:
                item = self._connector.ensure_folder(self._drive_id, parent_id, part)
                self._folder_ids[walked] = str(item["id"])
            parent_id = self._folder_ids[walked]
        return parent_id

    def upload(self, folder_path: str, filename: str, content: bytes) -> str:
        folder_id = self.ensure_folder(folder_path)
        item = self._connector.upload_bytes(self._drive_id, folder_id, filename, content)
        return str(item.get("id", filename))

    def download(self, ref: str) -> bytes:
        return bytes(self._connector.download_bytes(self._drive_id, ref))

    def list_files(self, folder_path: str) -> list[WorkspaceFile]:
        folder_id = self.ensure_folder(folder_path)
        children = self._connector.list_children(self._drive_id, folder_id)
        return [
            WorkspaceFile(
                name=str(c.get("name", "")),
                ref=str(c.get("id", "")),
                modified_at=c.get("lastModifiedDateTime"),
            )
            for c in children
            if "folder" not in c
        ]


# ------------------------------------------------------------ naming template


def slugify(text: str, max_len: int = 24) -> str:
    """Short slug — deliberately capped: campaign folder + asset filename + the
    session path must stay inside Windows' 260-char MAX_PATH in dev."""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "campaign"


def campaign_folder_name(config: OrchestratorConfig, topic: str, window_start: str) -> str:
    """``{year}-Q{quarter}-{slug}`` from the versioned naming template."""
    start = date.fromisoformat(window_start)
    quarter = (start.month - 1) // 3 + 1
    return (
        config.naming.campaign_folder.replace("{year}", str(start.year))
        .replace("{quarter}", str(quarter))
        .replace("{slug}", slugify(topic))
    )


def asset_filename(
    config: OrchestratorConfig, campaign_slug: str, asset_type: str, version: int
) -> str:
    return (
        config.naming.asset_file.replace("{campaign_slug}", campaign_slug)
        .replace("{asset_type}", asset_type.replace("_", "-"))
        .replace("{version}", str(version))
    )


def create_campaign_workspace(
    workspace: CampaignWorkspace, config: OrchestratorConfig, folder_name: str
) -> dict[str, str]:
    """Create the standardized folder tree from the versioned template.
    Returns folder path → ref. Idempotent (ensure semantics)."""
    refs = {folder_name: workspace.ensure_folder(folder_name)}
    for sub in config.workspace_folders:
        path = f"{folder_name}/{sub}"
        refs[path] = workspace.ensure_folder(path)
    return refs
