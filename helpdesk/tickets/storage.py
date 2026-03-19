from __future__ import annotations

import logging
import mimetypes

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage
from django.utils.deconstruct import deconstructible

from .minio import get_minio_config, get_s3_client

logger = logging.getLogger(__name__)

try:
    from botocore.exceptions import ClientError  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    ClientError = None  # type: ignore


@deconstructible
class TicketImageStorage(Storage):
    """
    Stores Ticket.image in MinIO (S3) when configured.

    Falls back to the local filesystem MEDIA_ROOT so existing uploads keep working
    even after switching to MinIO.
    """

    def __init__(self):
        self._fallback = FileSystemStorage(location=settings.MEDIA_ROOT, base_url=settings.MEDIA_URL)

    def _minio_enabled(self) -> bool:
        try:
            get_s3_client()
        except Exception:
            return False
        return True

    @staticmethod
    def _normalize_name(name: str) -> str:
        return (name or "").replace("\\", "/")

    def _minio_bucket(self) -> str:
        cfg = get_minio_config()
        return cfg.bucket

    def _s3(self):
        return get_s3_client()

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        if ClientError is not None and isinstance(exc, ClientError):
            code = str(exc.response.get("Error", {}).get("Code", "")).lower()
            return code in {"404", "nosuchkey", "notfound", "no such key"}
        return False

    def _minio_exists(self, name: str) -> bool:
        if not self._minio_enabled():
            return False
        try:
            self._s3().head_object(Bucket=self._minio_bucket(), Key=name)
            return True
        except Exception as exc:
            if self._is_not_found(exc):
                return False
            logger.warning("MinIO exists() check failed for %s: %s", name, exc)
            return False

    def _upload_to_minio(self, name: str, content) -> None:
        if hasattr(content, "seek"):
            try:
                content.seek(0)
            except Exception:
                pass

        content_type = getattr(content, "content_type", None) or mimetypes.guess_type(name)[0]
        content_type = content_type or "application/octet-stream"
        self._s3().upload_fileobj(
            content,
            self._minio_bucket(),
            name,
            ExtraArgs={"ContentType": content_type},
        )

    def _read_from_minio(self, name: str) -> bytes:
        obj = self._s3().get_object(Bucket=self._minio_bucket(), Key=name)
        body = obj["Body"]
        try:
            return body.read()
        finally:
            try:
                body.close()
            except Exception:
                pass

    def _delete_from_minio(self, name: str) -> None:
        self._s3().delete_object(Bucket=self._minio_bucket(), Key=name)

    def save(self, name, content, max_length=None):
        name = self._normalize_name(name)
        name = self.get_available_name(name, max_length=max_length)

        if self._minio_enabled():
            self._upload_to_minio(name, content)
            return name

        return self._fallback.save(name, content, max_length=max_length)

    def open(self, name, mode="rb"):
        name = self._normalize_name(name)
        if "r" not in mode:
            raise ValueError("TicketImageStorage only supports read modes.")

        if self._minio_exists(name):
            data = self._read_from_minio(name)
            return ContentFile(data, name=name)

        return self._fallback.open(name, mode=mode)

    def delete(self, name):
        name = self._normalize_name(name)
        if not name:
            return

        if self._minio_enabled():
            try:
                self._delete_from_minio(name)
            except Exception as exc:
                if not self._is_not_found(exc):
                    logger.warning("MinIO delete failed for %s: %s", name, exc)

        try:
            self._fallback.delete(name)
        except Exception:
            pass

    def exists(self, name):
        name = self._normalize_name(name)
        return self._minio_exists(name) or self._fallback.exists(name)

    def size(self, name):
        name = self._normalize_name(name)
        if self._minio_enabled():
            try:
                head = self._s3().head_object(Bucket=self._minio_bucket(), Key=name)
                return int(head.get("ContentLength") or 0)
            except Exception:
                pass
        return self._fallback.size(name)

    def url(self, name):
        name = self._normalize_name(name)
        if self._fallback.exists(name):
            return self._fallback.url(name)
        return "#"
