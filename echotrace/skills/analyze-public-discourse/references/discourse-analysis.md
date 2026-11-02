# Discourse coding guidance

Code each record as original post, comment, reply, quote, or repost. Keep text, record ID, parent ID, platform, published time, and collection time. Mark whether a statement is first-hand experience, attributed report, opinion, question, or factual claim. Preserve verbatim excerpts and avoid exposing personal identifiers.

Count independent origins only when author/source identity or a distinct reporting path
is present. Different record IDs, platforms, or timestamps alone are insufficient; use
`unknown` or a lower bound when identity is missing. Imported datasets may call the
record identifier `id`; preserve it when normalizing to the canonical `record_id` field.
