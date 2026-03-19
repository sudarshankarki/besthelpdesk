from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

try:
    import boto3  # type: ignore
    from botocore.config import Config  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    boto3 = None  # type: ignore
    Config = None  # type: ignore


@dataclass(frozen=True)
class MinIOConfig:
    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    region_name: str = "us-east-1"


def get_minio_config() -> MinIOConfig:
    endpoint_url = getattr(settings, "MINIO_ENDPOINT_URL", "").strip()
    access_key = getattr(settings, "MINIO_ACCESS_KEY", "").strip()
    secret_key = getattr(settings, "MINIO_SECRET_KEY", "").strip()
    bucket = getattr(settings, "MINIO_BUCKET", "").strip()
    region_name = getattr(settings, "MINIO_REGION", "us-east-1").strip() or "us-east-1"

    if not endpoint_url or not access_key or not secret_key or not bucket:
        raise RuntimeError("MinIO is not configured (MINIO_ENDPOINT_URL/MINIO_ACCESS_KEY/MINIO_SECRET_KEY/MINIO_BUCKET).")

    return MinIOConfig(
        endpoint_url=endpoint_url,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        region_name=region_name,
    )


def get_s3_client():
    if boto3 is None or Config is None:
        raise RuntimeError("boto3/botocore is not installed. Install it with: pip install boto3")
    cfg = get_minio_config()
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
        region_name=cfg.region_name,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
