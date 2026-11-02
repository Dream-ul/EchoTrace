# Collection modes

| Mode | Use | Required result |
| --- | --- | --- |
| discover | Build a candidate list from search or feeds | candidates plus query/date/platform scope; no evidence claim |
| collect | Fetch and preserve selected sources | RawCapture and normalized records with provenance |
| refresh | Add new material to an existing collection | idempotent merge, duplicate clusters, coverage delta |

Never silently move from discover to evidence. A snippet or URL is a lead until the body, retrieval metadata, and canonical identity are captured.
