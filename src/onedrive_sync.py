"""
onedrive_sync.py
Sync CSV and template files to/from OneDrive via Microsoft Graph API.

Required env vars:
    AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET
    ONEDRIVE_USER_ID  — the UPN or object ID of the OneDrive owner
                        e.g. "outreach@yourcompany.com"
"""

import logging
import os
from pathlib import Path

import msal
import requests

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _get_token() -> str:
    app = msal.ConfidentialClientApplication(
        os.environ["AZURE_CLIENT_ID"],
        authority=f"https://login.microsoftonline.com/{os.environ['AZURE_TENANT_ID']}",
        client_credential=os.environ["AZURE_CLIENT_SECRET"],
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        raise RuntimeError(f"Token error: {result.get('error_description')}")
    return result["access_token"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Download ──────────────────────────────────────────────────────────────────

def download_file(remote_path: str, local_path: str) -> None:
    """
    Download a file from OneDrive.

    Args:
        remote_path: Path inside OneDrive, e.g. "email-automation/contacts.csv"
        local_path:  Where to save it locally.
    """
    token    = _get_token()
    user_id  = os.environ["ONEDRIVE_USER_ID"]
    url      = f"{GRAPH_BASE}/users/{user_id}/drive/root:/{remote_path}:/content"

    resp = requests.get(url, headers=_headers(token), timeout=60)
    resp.raise_for_status()

    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    Path(local_path).write_bytes(resp.content)
    log.info("Downloaded %s → %s (%d bytes)", remote_path, local_path, len(resp.content))


def download_folder(remote_folder: str, local_folder: str, extension: str = "") -> None:
    """
    Download all files in a OneDrive folder (non-recursive).
    Optionally filter by file extension, e.g. ".html".
    """
    token   = _get_token()
    user_id = os.environ["ONEDRIVE_USER_ID"]
    url     = f"{GRAPH_BASE}/users/{user_id}/drive/root:/{remote_folder}:/children"

    resp = requests.get(url, headers=_headers(token), timeout=30)
    resp.raise_for_status()

    items = resp.json().get("value", [])
    Path(local_folder).mkdir(parents=True, exist_ok=True)

    for item in items:
        name = item.get("name", "")
        if extension and not name.endswith(extension):
            continue
        if "file" in item:
            download_file(f"{remote_folder}/{name}", str(Path(local_folder) / name))


# ── Upload ────────────────────────────────────────────────────────────────────

def upload_file(local_path: str, remote_path: str) -> None:
    """
    Upload a file to OneDrive (overwrites if exists).

    Args:
        local_path:  Local file to upload.
        remote_path: Destination path in OneDrive, e.g. "email-automation/contacts.csv"
    """
    token    = _get_token()
    user_id  = os.environ["ONEDRIVE_USER_ID"]
    url      = f"{GRAPH_BASE}/users/{user_id}/drive/root:/{remote_path}:/content"

    data = Path(local_path).read_bytes()
    resp = requests.put(url, headers={**_headers(token), "Content-Type": "application/octet-stream"},
                        data=data, timeout=120)
    resp.raise_for_status()
    log.info("Uploaded %s → %s", local_path, remote_path)


def upload_folder(local_folder: str, remote_folder: str, extension: str = "") -> None:
    """Upload all files in a local folder to a OneDrive folder."""
    for p in Path(local_folder).iterdir():
        if p.is_file() and (not extension or p.suffix == extension):
            upload_file(str(p), f"{remote_folder}/{p.name}")


# ── Convenience wrappers used by the workflow ─────────────────────────────────

def sync_down(remote_base: str, local_base: str) -> None:
    """Pull CSVs and templates from OneDrive before the run."""
    log.info("Syncing DOWN from OneDrive folder: %s", remote_base)
    download_folder(f"{remote_base}/csv",       f"{local_base}/csv",       ".csv")
    download_folder(f"{remote_base}/templates", f"{local_base}/templates", ".html")


def sync_up(remote_base: str, local_base: str) -> None:
    """Push updated CSVs back to OneDrive after the run."""
    log.info("Syncing UP to OneDrive folder: %s", remote_base)
    upload_folder(f"{local_base}/csv",            f"{remote_base}/csv",       ".csv")
    upload_folder(f"{local_base}/csv/backups",    f"{remote_base}/backups",   ".csv")
    upload_folder(f"{local_base}/logs",           f"{remote_base}/logs",      ".log")