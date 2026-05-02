"""
onedrive_sync.py
Sync CSV and template files to/from OneDrive via Microsoft Graph API.

Changes from Phase 1
--------------------
- Token is acquired once per sync_down/sync_up call and reused across all
  file operations — avoids a round-trip per file.
- download_folder now raises a clear, descriptive error when the remote
  folder is not found (404), rather than letting the run silently produce
  no files and fail later with a confusing FileNotFoundError.
- upload_folder now skips gracefully if the local folder doesn't exist
  (e.g. backups/ on the first run of the month) instead of crashing.
- All Graph API errors now include the HTTP status and response body in
  the exception message to make debugging easier.

Required env vars:
    AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET
    ONEDRIVE_USER_ID  — UPN of the OneDrive owner, e.g. "you@company.com"
"""

import logging
import os
from pathlib import Path

import msal
import requests

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_token() -> str:
    """Acquire a client-credentials OAuth2 token. Call once per workflow step."""
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


def _raise_for_status(resp: requests.Response, context: str) -> None:
    """Raise with a clear message including context, status, and response body."""
    if not resp.ok:
        raise RuntimeError(
            f"{context} failed — HTTP {resp.status_code}: {resp.text[:400]}"
        )


# ── Download ──────────────────────────────────────────────────────────────────

def download_file(token: str, remote_path: str, local_path: str) -> None:
    """
    Download a single file from OneDrive.

    Args:
        token:       Access token (reuse across calls — don't re-fetch per file).
        remote_path: Path inside OneDrive, e.g. "email-automation/csv/contacts.csv"
        local_path:  Local destination path.
    """
    user_id = os.environ["ONEDRIVE_USER_ID"]
    url     = f"{GRAPH_BASE}/users/{user_id}/drive/root:/{remote_path}:/content"

    resp = requests.get(url, headers=_headers(token), timeout=60)
    _raise_for_status(resp, f"Download '{remote_path}'")

    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    Path(local_path).write_bytes(resp.content)
    log.info("Downloaded  %s  →  %s  (%d bytes)", remote_path, local_path, len(resp.content))


def download_folder(
    token: str,
    remote_folder: str,
    local_folder: str,
    extension: str = "",
) -> None:
    """
    Download all files in a OneDrive folder (non-recursive).

    Args:
        token:         Access token.
        remote_folder: OneDrive folder path, e.g. "email-automation/csv"
        local_folder:  Local destination folder.
        extension:     If set, only download files with this extension, e.g. ".csv"

    Raises:
        RuntimeError: If the remote folder is not found or the API call fails.
                      A 404 here almost always means ONEDRIVE_REMOTE_BASE or the
                      folder name doesn't match what's in OneDrive exactly.
    """
    user_id = os.environ["ONEDRIVE_USER_ID"]
    url     = f"{GRAPH_BASE}/users/{user_id}/drive/root:/{remote_folder}:/children"

    resp = requests.get(url, headers=_headers(token), timeout=30)

    # Give a specific, actionable message for the most common failure
    if resp.status_code == 404:
        raise RuntimeError(
            f"OneDrive folder not found: '{remote_folder}'\n"
            f"Check that ONEDRIVE_REMOTE_BASE is set correctly and that the folder "
            f"exists in OneDrive under the account '{os.environ.get('ONEDRIVE_USER_ID')}'.\n"
            f"Folder paths are case-sensitive via the Graph API."
        )
    _raise_for_status(resp, f"List folder '{remote_folder}'")

    items = resp.json().get("value", [])
    if not items:
        log.warning("OneDrive folder '%s' exists but contains no files.", remote_folder)
        return

    Path(local_folder).mkdir(parents=True, exist_ok=True)

    downloaded = 0
    for item in items:
        name = item.get("name", "")
        if extension and not name.endswith(extension):
            continue
        if "file" in item:
            download_file(token, f"{remote_folder}/{name}", str(Path(local_folder) / name))
            downloaded += 1

    log.info(
        "Folder '%s' — %d file(s) downloaded to '%s'",
        remote_folder, downloaded, local_folder,
    )


# ── Upload ────────────────────────────────────────────────────────────────────

def upload_file(token: str, local_path: str, remote_path: str) -> None:
    """
    Upload a single file to OneDrive (overwrites if it already exists).

    Args:
        token:       Access token.
        local_path:  Local file to upload.
        remote_path: Destination path in OneDrive.
    """
    user_id = os.environ["ONEDRIVE_USER_ID"]
    url     = f"{GRAPH_BASE}/users/{user_id}/drive/root:/{remote_path}:/content"

    data = Path(local_path).read_bytes()
    resp = requests.put(
        url,
        headers={**_headers(token), "Content-Type": "application/octet-stream"},
        data=data,
        timeout=120,
    )
    _raise_for_status(resp, f"Upload '{local_path}' → '{remote_path}'")
    log.info("Uploaded  %s  →  %s", local_path, remote_path)


def upload_folder(
    token: str,
    local_folder: str,
    remote_folder: str,
    extension: str = "",
) -> None:
    """
    Upload all files in a local folder to a OneDrive folder.
    Skips silently if the local folder doesn't exist (e.g. backups/ on first run).
    """
    local_path = Path(local_folder)

    if not local_path.exists():
        log.info("Skipping upload — local folder does not exist: %s", local_folder)
        return

    uploaded = 0
    for p in local_path.iterdir():
        if p.is_file() and (not extension or p.suffix == extension):
            upload_file(token, str(p), f"{remote_folder}/{p.name}")
            uploaded += 1

    log.info(
        "Folder '%s' — %d file(s) uploaded to '%s'",
        local_folder, uploaded, remote_folder,
    )


# ── Convenience wrappers ──────────────────────────────────────────────────────

def sync_down(remote_base: str, local_base: str) -> None:
    """
    Pull all CSVs and templates from OneDrive before the run.
    Acquires a single token and reuses it for all downloads.
    """
    log.info("Syncing DOWN from OneDrive: %s", remote_base)
    token = _get_token()
    download_folder(token, f"{remote_base}/csv",       f"{local_base}/csv",       ".csv")
    download_folder(token, f"{remote_base}/templates", f"{local_base}/templates", ".html")


def sync_up(remote_base: str, local_base: str) -> None:
    """
    Push updated CSVs and logs back to OneDrive after the run.
    Acquires a single token and reuses it for all uploads.
    """
    log.info("Syncing UP to OneDrive: %s", remote_base)
    token = _get_token()
    upload_folder(token, f"{local_base}/csv",         f"{remote_base}/csv",     ".csv")
    upload_folder(token, f"{local_base}/csv/backups", f"{remote_base}/backups", ".csv")
    upload_folder(token, f"{local_base}/logs",        f"{remote_base}/logs",    ".log")
