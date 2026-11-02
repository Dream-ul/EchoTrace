# Source provenance

Keep provider and request metadata internal to the harness, but retain source URL, external ID, platform, publisher identity, published time, retrieved time, canonical URL, content hash, and parent/repost relation. A redirect or syndicated copy should point to its canonical origin when known.

Canonical identity is an acceptance gate, not an optional enrichment field: if a
reproducible `http(s)` canonical URL is unavailable, retain the attempt as a raw
capture or quarantine record and do not promote it into `normalized_records`.

For discovery or planning runs, use explicit null/unknown fields and a planned or
not_attempted status. Never synthesize timestamps, hashes, response codes, or publisher
identity from a candidate URL or snippet.
