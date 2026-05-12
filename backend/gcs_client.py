"""
GCS Client — wraps google-cloud-storage for anonymous access to
the public NOAA passive bioacoustic bucket.
"""

import asyncio
from pathlib import Path

from google.cloud import storage
from google.auth.credentials import AnonymousCredentials

AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".aif", ".aiff"}
MAX_ITEMS = 200  # max files to return per browse call


class GCSClient:
    def __init__(self, bucket_name: str):
        self.bucket_name = bucket_name
        self._client = storage.Client(
            credentials=AnonymousCredentials(),
            project=bucket_name,
        )
        self._bucket = self._client.bucket(bucket_name)

    # ── Listing helpers ────────────────────────────────────────────────────────

    async def list_stations(self) -> list[dict]:
        return await asyncio.to_thread(self._list_stations_sync)

    def _list_stations_sync(self) -> list[dict]:
        iterator = self._client.list_blobs(
            self.bucket_name, prefix="", delimiter="/"
        )
        prefixes = []
        for page in iterator.pages:
            for prefix in page.prefixes:
                name = prefix.rstrip("/")
                prefixes.append({"name": name, "prefix": prefix, "type": "folder"})
            if len(prefixes) >= MAX_ITEMS:
                break
        return sorted(prefixes, key=lambda x: x["name"])

    async def browse(self, prefix: str = "") -> dict:
        # 30-second timeout so the UI never hangs indefinitely
        return await asyncio.wait_for(
            asyncio.to_thread(self._browse_sync, prefix),
            timeout=30.0
        )

    def _browse_sync(self, prefix: str) -> dict:
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        iterator = self._client.list_blobs(
            self.bucket_name, prefix=prefix, delimiter="/"
        )

        folders = []
        files = []

        for page in iterator.pages:
            for p in sorted(page.prefixes):
                label = p[len(prefix):].rstrip("/")
                folders.append({"name": label, "prefix": p, "type": "folder"})

            for blob in page:
                if blob.name == prefix:
                    continue
                suffix = Path(blob.name).suffix.lower()
                if suffix not in AUDIO_EXTENSIONS:
                    continue
                files.append({
                    "name": Path(blob.name).name,
                    "path": blob.name,
                    "size_bytes": blob.size,
                    "size_mb": round((blob.size or 0) / 1e6, 2),
                    "updated": blob.updated.isoformat() if blob.updated else None,
                    "content_type": blob.content_type,
                    "type": "file",
                    "extension": suffix.lstrip("."),
                })

            # Stop early if we have enough
            if len(folders) + len(files) >= MAX_ITEMS:
                break

        files.sort(key=lambda x: x["name"])
        return {
            "prefix": prefix,
            "folders": folders,
            "files": files,
            "truncated": len(folders) + len(files) >= MAX_ITEMS
        }

    async def get_file_info(self, path: str) -> dict:
        return await asyncio.to_thread(self._get_file_info_sync, path)

    def _get_file_info_sync(self, path: str) -> dict:
        blob = self._bucket.blob(path)
        blob.reload()
        return {
            "name": Path(path).name,
            "path": path,
            "size_bytes": blob.size,
            "size_mb": round((blob.size or 0) / 1e6, 2),
            "updated": blob.updated.isoformat() if blob.updated else None,
            "content_type": blob.content_type,
            "md5": blob.md5_hash,
        }

    async def download_file(self, gcs_path: str, local_path: Path) -> None:
        await asyncio.to_thread(self._download_sync, gcs_path, local_path)

    def _download_sync(self, gcs_path: str, local_path: Path) -> None:
        blob = self._bucket.blob(gcs_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local_path))
