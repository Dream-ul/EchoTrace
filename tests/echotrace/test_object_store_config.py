from __future__ import annotations

import pytest

from echotrace.infrastructure.object_store_config import ObjectStoreConfig


def test_object_store_config_requires_production_endpoint_and_credentials():
    with pytest.raises(RuntimeError, match="S3_ENDPOINT_URL"):
        ObjectStoreConfig.from_env({})


def test_object_store_config_rejects_tls_scheme_mismatch():
    values = {
        "S3_ENDPOINT_URL": "https://rustfs.example",
        "S3_BUCKET": "echotrace-sources",
        "S3_ACCESS_KEY_ID": "access",
        "S3_SECRET_ACCESS_KEY": "secret",
        "S3_USE_TLS": "0",
    }
    with pytest.raises(RuntimeError, match="S3_USE_TLS"):
        ObjectStoreConfig.from_env(values)


def test_object_store_config_parses_rustfs_defaults():
    values = {
        "S3_ENDPOINT_URL": "http://rustfs:9000/",
        "S3_BUCKET": "echotrace-sources",
        "S3_ACCESS_KEY_ID": "access",
        "S3_SECRET_ACCESS_KEY": "secret",
    }
    config = ObjectStoreConfig.from_env(values)
    assert config.endpoint_url == "http://rustfs:9000"
    assert config.use_tls is False
    assert config.max_object_bytes == 50 * 1024 * 1024
