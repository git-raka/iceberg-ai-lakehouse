"""Access to the lakehouse from Python: the Iceberg REST catalog (PyIceberg), S3 files and Spark Connect."""

from __future__ import annotations

from functools import lru_cache

import pyarrow as pa
import pyarrow.fs as pafs
from pyiceberg.catalog import Catalog, load_catalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError

from .settings import settings


@lru_cache
def catalog() -> Catalog:
    s = settings()
    return load_catalog(
        "lakehouse",
        **{
            "type": "rest",
            "uri": s.catalog_uri,
            "warehouse": s.warehouse,
            "s3.endpoint": s.s3_endpoint,
            "s3.access-key-id": s.s3_access_key,
            "s3.secret-access-key": s.s3_secret_key,
            "s3.region": "us-east-1",
            "s3.path-style-access": "true",
        },
    )


def ensure_namespace(name: str) -> None:
    try:
        catalog().create_namespace(name)
    except NamespaceAlreadyExistsError:
        pass


def append(identifier: str, data: pa.Table) -> int:
    """Append an Arrow table to an Iceberg table, creating it (with the Arrow schema) on first use."""
    if data.num_rows == 0:
        return 0
    ensure_namespace(identifier.split(".")[0])
    try:
        table = catalog().load_table(identifier)
    except NoSuchTableError:
        table = catalog().create_table(identifier, schema=data.schema)
    table.append(data)
    return data.num_rows


def read_arrow(identifier: str, row_filter: str = "true", columns: tuple[str, ...] = ("*",)) -> pa.Table:
    return catalog().load_table(identifier).scan(row_filter=row_filter, selected_fields=columns).to_arrow()


def table_exists(identifier: str) -> bool:
    try:
        catalog().load_table(identifier)
        return True
    except NoSuchTableError:
        return False


@lru_cache
def s3() -> pafs.S3FileSystem:
    s = settings()
    return pafs.S3FileSystem(
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key,
        endpoint_override=s.s3_endpoint,
        scheme="http" if s.s3_endpoint.startswith("http://") else "https",
        region="us-east-1",
        force_virtual_addressing=False,
    )


def s3_key(*parts: str) -> str:
    """Bucket-relative key in the form pyarrow expects: bucket/path."""
    return "/".join([settings().bucket, *[p.strip("/") for p in parts]])


def put_bytes(key: str, data: bytes) -> None:
    with s3().open_output_stream(key) as out:
        out.write(data)


def get_bytes(key: str) -> bytes:
    with s3().open_input_stream(key) as f:
        return f.read()


def list_files(prefix: str) -> list[pafs.FileInfo]:
    selector = pafs.FileSelector(prefix, recursive=True, allow_not_found=True)
    return [f for f in s3().get_file_info(selector) if f.type == pafs.FileType.File]


def spark():
    """A Spark Connect session to the shared Spark server (Iceberg catalog `lakehouse` preconfigured)."""
    from pyspark.sql import SparkSession

    return SparkSession.builder.remote(settings().spark_remote).getOrCreate()
