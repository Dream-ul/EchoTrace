from echotrace.ingestion import DocumentNormalizer, RssSourceAdapter


RSS = """
<rss version="2.0"><channel><item>
  <title>产品争议更新</title>
  <link>https://example.test/item-1</link>
  <description>用户反馈售后响应很慢。</description>
  <pubDate>Tue, 09 Sep 2026 08:00:00 GMT</pubDate>
  <author>reporter@example.test</author>
</item><item>
  <title>重复转载</title><link>https://example.test/item-2</link>
  <description>用户反馈售后响应很慢。</description>
</item></channel></rss>
"""


def test_rss_parser_maps_entries_and_normalizer_deduplicates():
    payloads = RssSourceAdapter(source="public_rss").parse(RSS, feed_url="https://example.test/feed.xml")

    assert payloads[0]["title"] == "产品争议更新"
    assert payloads[0]["url"] == "https://example.test/item-1"
    assert payloads[0]["published_at"].year == 2026
    assert len(DocumentNormalizer().normalize_many(payloads)) == 1


def test_atom_parser_supports_iso_dates_and_limit():
    atom = """
    <feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <title>官方回应</title><link href="https://example.test/a"/>
      <updated>2026-09-09T10:00:00Z</updated><content>官方回应称信息不确定。</content>
      <author><name>官方账号</name></author>
    </entry><entry><title>第二条</title><content>补充材料</content></entry></feed>
    """
    payloads = RssSourceAdapter().parse(atom, limit=1)

    assert len(payloads) == 1
    assert payloads[0]["author"] == "官方账号"
    assert payloads[0]["published_at"].isoformat() == "2026-09-09T10:00:00+00:00"
