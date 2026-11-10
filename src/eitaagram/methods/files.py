"""Uploads, media sending and downloads.

Beta: the part-size and big-file thresholds follow the reference captures but
were not verified live from this code base -- run ``examples/smoke_test.py``
against a throw-away account before relying on it.
"""

from __future__ import annotations

import asyncio
import math
import mimetypes
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ClientError, TransportError
from ..peers import PeerRef
from ..types import Message
from ..utils import random_id
from .messages import _chat_for_input, _parse, messages_from_updates

if TYPE_CHECKING:  # pragma: no cover
    from ..client import Client

BIG_FILE_THRESHOLD = 10 * 1024 * 1024
MAX_UPLOAD_PARTS = 4000
Progress = Callable[[int, int], Awaitable[None] | None]
Source = str | os.PathLike[str] | bytes


def _part_size(size: int) -> int:
    if size > 64 * 1024 * 1024:
        return 512 * 1024
    if size < 100 * 1024:
        return 32 * 1024
    return 256 * 1024


def _plain_peer(client: Client, peer: dict[str, Any]) -> dict[str, Any] | None:
    kind = peer["_"]
    if kind == "inputPeerUser":
        return {"_": "peerUser", "user_id": peer["user_id"]}
    if kind == "inputPeerChat":
        return {"_": "peerChat", "chat_id": peer["chat_id"]}
    if kind == "inputPeerChannel":
        return {"_": "peerChannel", "channel_id": peer["channel_id"]}
    if kind == "inputPeerSelf" and client.session.data.user_id:
        return {"_": "peerUser", "user_id": client.session.data.user_id}
    return None


def _uploaded_media(
    name: str, uploaded: dict[str, Any], mime: str, *, as_document: bool, voice: bool, duration: int, width: int, height: int
) -> dict[str, Any]:
    if mime.startswith("image/") and mime not in ("image/gif", "image/webp") and not as_document:
        return {"_": "inputMediaUploadedPhoto", "file": uploaded}
    attributes: list[dict[str, Any]] = [{"_": "documentAttributeFilename", "file_name": name}]
    if voice or mime.startswith("audio/"):
        attributes.append({"_": "documentAttributeAudio", "voice": voice, "duration": max(0, duration)})
    elif mime.startswith("video/"):
        attributes.append(
            {"_": "documentAttributeVideo", "supports_streaming": True, "duration": max(0, duration), "w": max(0, width), "h": max(0, height)}
        )
    return {"_": "inputMediaUploadedDocument", "file": uploaded, "mime_type": mime, "attributes": attributes, "force_file": as_document}


class FileMethods:
    async def upload_file(
        self: Client,
        source: Source,
        *,
        name: str | None = None,
        peer: dict[str, Any] | None = None,
        progress: Progress | None = None,
    ) -> dict[str, Any]:
        """Upload bytes or a file; returns an ``inputFile`` / ``inputFileBig`` dict."""
        if isinstance(source, bytes):
            data: bytes | None = source
            size = len(source)
            filename = name or "file"
        else:
            path = Path(source).expanduser()
            if not await asyncio.to_thread(path.is_file):
                raise FileNotFoundError(path)
            data, size, filename = None, (await asyncio.to_thread(path.stat)).st_size, name or path.name
        part_size = _part_size(size)
        parts = max(1, math.ceil(size / part_size))
        if parts > MAX_UPLOAD_PARTS:
            raise ClientError(f"file needs {parts} parts; the limit is {MAX_UPLOAD_PARTS}")
        big = size >= BIG_FILE_THRESHOLD
        plain = _plain_peer(self, peer) if peer else None

        last_error: TransportError | None = None
        for _attempt in range(2):  # a failed host restarts the whole upload on another one
            endpoint = self.transport.pick_endpoint("upload")
            file_id = random_id() or 1
            try:
                handle = None if data is not None else await asyncio.to_thread(Path(source).expanduser().open, "rb")  # type: ignore[arg-type]
                try:
                    for index in range(parts):
                        chunk = data[index * part_size : (index + 1) * part_size] if data is not None else await asyncio.to_thread(handle.read, part_size)  # type: ignore[union-attr]
                        params: dict[str, Any] = {"file_id": file_id, "file_part": index, "bytes": chunk, "totalFileSize": size}
                        if plain is not None:
                            params["peer"] = plain
                        if big:
                            params["file_total_parts"] = parts
                            await self.invoke("upload.saveBigFilePart", params, kind="upload", endpoint=endpoint)
                        else:
                            await self.invoke("upload.saveFilePart", params, kind="upload", endpoint=endpoint)
                        if progress is not None:
                            result = progress(min((index + 1) * part_size, size), size)
                            if asyncio.iscoroutine(result):
                                await result
                finally:
                    if handle is not None:
                        await asyncio.to_thread(handle.close)
            except TransportError as exc:
                last_error = exc
                continue
            if big:
                return {"_": "inputFileBig", "id": file_id, "parts": parts, "name": filename}
            return {"_": "inputFile", "id": file_id, "parts": parts, "name": filename, "md5_checksum": ""}
        assert last_error is not None
        raise last_error

    async def send_file(
        self: Client,
        chat: PeerRef,
        source: Source,
        *,
        caption: str = "",
        name: str | None = None,
        reply_to: int | None = None,
        parse_mode: str | None = None,
        silent: bool = False,
        as_document: bool = False,
        voice: bool = False,
        duration: int = 0,
        width: int = 0,
        height: int = 0,
        progress: Progress | None = None,
    ) -> Message:
        peer = await self.resolver.resolve(chat)
        uploaded = await self.upload_file(source, name=name, peer=peer, progress=progress)
        filename = uploaded["name"]
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        media = _uploaded_media(filename, uploaded, mime, as_document=as_document, voice=voice, duration=duration, width=width, height=height)
        text, entities = _parse(caption, parse_mode, None)
        params: dict[str, Any] = {
            "peer": peer,
            "media": media,
            "message": text,
            "random_id": random_id(),
            "silent": silent,
            "reply_to_msg_id": reply_to,
            "entities": entities,
        }
        result = await self.invoke("messages.sendMedia", {k: v for k, v in params.items() if v is not None})
        found = messages_from_updates(result, self)
        if found:
            return found[0]
        return Message(id=0, chat=_chat_for_input(self, peer), text=text, outgoing=True, raw=result if isinstance(result, dict) else {}, client=self)

    async def send_photo(self: Client, chat: PeerRef, source: Source, **kwargs: Any) -> Message:
        return await self.send_file(chat, source, **kwargs)

    async def send_document(self: Client, chat: PeerRef, source: Source, **kwargs: Any) -> Message:
        kwargs["as_document"] = True
        return await self.send_file(chat, source, **kwargs)

    async def download_media(
        self: Client, message: Message, destination: str | os.PathLike[str] = ".", *, progress: Progress | None = None
    ) -> Path:
        """Download a photo / document attached to *message*; returns the written path."""
        media = message.raw.get("media")
        if not media:
            raise ClientError("this message has no media")
        location, size, suggested = _location_from_media(media)
        target = Path(destination).expanduser()
        if await asyncio.to_thread(target.is_dir) or (target.suffix == "" and not target.exists()):
            await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
            target = target / suggested
        else:
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        offset = 0
        handle = await asyncio.to_thread(partial.open, "wb")
        try:
            while offset < size:
                response = await self.invoke(
                    "upload.getFile",
                    {"location": location, "offset": offset, "limit": min(512 * 1024, size - offset), "cdn_supported": False},
                    kind="download",
                )
                if response.get("_") != "upload.file":
                    raise ClientError(f"unsupported download response: {response.get('_')}")
                chunk = response.get("bytes", b"")
                if not chunk:
                    break
                await asyncio.to_thread(handle.write, chunk)
                offset += len(chunk)
                if progress is not None:
                    result = progress(offset, size)
                    if asyncio.iscoroutine(result):
                        await result
        finally:
            await asyncio.to_thread(handle.close)
        if size and offset < size:
            await asyncio.to_thread(partial.unlink, missing_ok=True)
            raise ClientError(f"download stopped at {offset} of {size} bytes")
        await asyncio.to_thread(os.replace, partial, target)
        return target


def _location_from_media(media: dict[str, Any]) -> tuple[dict[str, Any], int, str]:
    kind = media.get("_")
    if kind == "messageMediaDocument" and media.get("document"):
        doc = media["document"]
        ident = int(doc["id"])
        filename = next(
            (a.get("file_name") for a in doc.get("attributes", []) if a.get("_") == "documentAttributeFilename" and a.get("file_name")),
            None,
        ) or f"document-{ident}{mimetypes.guess_extension(doc.get('mime_type', '')) or ''}"
        location = {
            "_": "inputDocumentFileLocation",
            "id": ident,
            "access_hash": doc["access_hash"],
            "file_reference": doc.get("file_reference", b""),
            "thumb_size": "",
        }
        return location, int(doc.get("size", 0)), str(filename)
    if kind == "messageMediaPhoto" and media.get("photo"):
        photo = media["photo"]

        def size_of(item: dict[str, Any]) -> int:
            if "size" in item:
                return int(item["size"])
            return max((int(s) for s in item.get("sizes", [])), default=0)

        sizes = [s for s in photo.get("sizes", []) if s.get("_") not in ("photoSizeEmpty", "photoStrippedSize", "photoCachedSize") or size_of(s)]
        sizes = [s for s in sizes if size_of(s) > 0]
        if not sizes:
            raise ClientError("photo has no downloadable size")
        best = max(sizes, key=size_of)
        ident = int(photo["id"])
        location = {
            "_": "inputPhotoFileLocation",
            "id": ident,
            "access_hash": photo["access_hash"],
            "file_reference": photo.get("file_reference", b""),
            "thumb_size": best.get("type", "x"),
        }
        return location, size_of(best), f"photo-{ident}.jpg"
    raise ClientError(f"message media {kind!r} cannot be downloaded")
