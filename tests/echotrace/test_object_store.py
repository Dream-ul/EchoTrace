import hashlib

import pytest

from echotrace.infrastructure.object_store import LocalObjectStore, ObjectStoreError
from echotrace.infrastructure.object_store import S3CompatibleObjectStore


def test_local_object_store_round_trip_and_content_hash(tmp_path):
    store = LocalObjectStore(tmp_path)
    payload = "中文证据".encode("utf-8")

    stored = store.put_bytes("run-1/images/evidence.png", payload, mime_type="image/png")

    assert stored.content_hash == hashlib.sha256(payload).hexdigest()
    assert stored.byte_size == len(payload)
    assert store.exists(stored.object_key)
    assert store.get_bytes(stored.object_key) == payload


def test_local_object_store_rejects_unsafe_keys_mime_and_size(tmp_path):
    store = LocalObjectStore(tmp_path, max_bytes=3)

    with pytest.raises(ObjectStoreError):
        store.put_bytes("../escape.txt", b"x", mime_type="text/plain")
    with pytest.raises(ObjectStoreError):
        store.put_bytes("file.csv", b"x", mime_type="text/csv")
    with pytest.raises(ObjectStoreError):
        store.put_bytes("large.txt", b"1234", mime_type="text/plain")


def test_local_object_store_validates_expected_hash(tmp_path):
    store = LocalObjectStore(tmp_path)

    with pytest.raises(ObjectStoreError, match="hash"):
        store.put_bytes("note.txt", b"hello", mime_type="text/plain", expected_hash="0" * 64)


class _FakeS3:
    def __init__(self):
        self.objects = {}

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise type("NotFound", (Exception,), {"response": {"Error": {"Code": "404"}}})()
        value = self.objects[Key]
        return {"Metadata": {"sha256": value[1]}, "ContentLength": len(value[0]), "ETag": '"etag"'}

    def put_object(self, *, Bucket, Key, Body, Metadata, **kwargs):
        if Key in self.objects:
            raise type("Precondition", (Exception,), {"response": {"ResponseMetadata": {"HTTPStatusCode": 412}}})()
        self.objects[Key] = (bytes(Body), Metadata["sha256"])
        return {"ETag": '"etag"'}

    def get_object(self, *, Bucket, Key):
        payload, digest = self.objects[Key]
        return {"Body": type("Body", (), {"read": lambda self: payload, "close": lambda self: None})(), "Metadata": {"sha256": digest}}


def test_s3_store_is_immutable_and_validates_readback_hash():
    store = S3CompatibleObjectStore(
        endpoint_url="http://rustfs:9000", bucket="bucket", access_key="access", secret_key="secret"
    )
    store._client = _FakeS3()
    stored = store.put_bytes("sources/doc.txt", b"hello", mime_type="text/plain")
    assert stored.content_hash == hashlib.sha256(b"hello").hexdigest()
    assert store.put_bytes("sources/doc.txt", b"hello", mime_type="text/plain").content_hash == stored.content_hash
    with pytest.raises(ObjectStoreError, match="immutable"):
        store.put_bytes("sources/doc.txt", b"other", mime_type="text/plain")
    assert store.get_bytes("sources/doc.txt") == b"hello"
