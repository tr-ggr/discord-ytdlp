"""Minimal async client for the storage.to upload API (https://storage.to/docs/api)."""

import logging
import os

import aiohttp

API_BASE = "https://storage.to/api"
DEFAULT_EXPIRY_DAYS = 3

log = logging.getLogger(__name__)


class StorageToError(Exception):
    def __init__(self, status: int, message: str, retry_after: str | None = None):
        self.status = status
        self.retry_after = retry_after
        detail = f"storage.to error {status}: {message}"
        if retry_after:
            detail += f" (retry after {retry_after}s)"
        super().__init__(detail)


class StorageToClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self._session = session
        self._auth = {"Authorization": f"Bearer {api_key}"}

    async def _api(self, method: str, path: str, *, json=None, owner_token: str | None = None) -> dict:
        headers = dict(self._auth, Accept="application/json")
        if owner_token:
            headers["X-Owner-Token"] = owner_token
        async with self._session.request(method, API_BASE + path, json=json, headers=headers) as resp:
            try:
                body = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError):
                body = {"error": (await resp.text())[:200]}
            if resp.status >= 400 or body.get("success") is False:
                raise StorageToError(resp.status, body.get("error", "unknown error"), resp.headers.get("Retry-After"))
            return body

    async def _put(self, url: str, data: bytes, headers: dict | None = None) -> aiohttp.ClientResponse:
        async with self._session.put(url, data=data, headers=headers or {}) as resp:
            if resp.status >= 400:
                raise StorageToError(resp.status, f"byte upload failed: {(await resp.text())[:200]}")
            return resp

    async def upload_file(
        self,
        path: str,
        filename: str,
        content_type: str = "audio/mpeg",
        expiry_days: int | None = None,
    ) -> dict:
        """Upload a local file and return storage.to's `file` object (id, url, human_size, expires_at)."""
        size = os.path.getsize(path)
        meta = {"filename": filename, "content_type": content_type, "size": size}

        init = await self._api("POST", "/upload/init", json=meta)
        if init.get("type") == "multipart":
            await self._upload_multipart(path, init)
        else:
            await self._upload_single(path, init)

        confirmed = await self._api("POST", "/upload/confirm", json={**meta, "r2_key": init["r2_key"]})
        file = confirmed["file"]

        if expiry_days and expiry_days != DEFAULT_EXPIRY_DAYS:
            try:
                await self._api(
                    "POST",
                    f"/file/{file['id']}/expiry",
                    json={"days": expiry_days},
                    owner_token=confirmed.get("owner_token"),
                )
                file["expiry_days"] = expiry_days
            except StorageToError as exc:
                log.warning("Could not set expiry on %s: %s", file["id"], exc)

        return file

    async def _upload_single(self, path: str, init: dict) -> None:
        # Presigned headers come back as {name: [values]}; Host is set by aiohttp from the URL.
        headers = {
            name: ", ".join(value) if isinstance(value, list) else str(value)
            for name, value in (init.get("headers") or {}).items()
            if name.lower() != "host"
        }
        with open(path, "rb") as f:
            data = f.read()
        await self._put(init["upload_url"], data, headers)

    async def _upload_multipart(self, path: str, init: dict) -> None:
        upload_id = init["upload_id"]
        owner_token = init.get("owner_token")
        part_size = init["part_size"]
        total_parts = init["total_parts"]
        urls = {int(n): url for n, url in (init.get("initial_urls") or {}).items()}

        try:
            missing = [n for n in range(1, total_parts + 1) if n not in urls]
            if missing:
                more = await self._api(
                    "POST",
                    "/upload/parts",
                    json={"upload_id": upload_id, "part_numbers": missing},
                    owner_token=owner_token,
                )
                urls.update({p["partNumber"]: p["url"] for p in more["part_urls"]})

            parts = []
            with open(path, "rb") as f:
                for n in range(1, total_parts + 1):
                    resp = await self._put(urls[n], f.read(part_size))
                    parts.append({"partNumber": n, "etag": resp.headers.get("ETag", "")})

            await self._api(
                "POST",
                "/upload/complete-multipart",
                json={"upload_id": upload_id, "parts": parts},
                owner_token=owner_token,
            )
        except Exception:
            try:
                await self._api("POST", "/upload/abort", json={"upload_id": upload_id}, owner_token=owner_token)
            except StorageToError as exc:
                log.warning("Failed to abort multipart upload %s: %s", upload_id, exc)
            raise
