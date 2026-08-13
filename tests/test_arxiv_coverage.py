"""arxiv.search / arxiv.get: query building, Atom XML parsing, error handling.

All httpx calls are mocked — no network. The fake AsyncClient records the GET
params the tool built and returns a canned Atom feed (or a canned failure).
"""
import asyncio

import httpx
import pytest

import tools.arxiv.search as M
from runtime.tool_base import ToolContext
from tools.arxiv.search import ArxivGet, ArxivSearch

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <entry>
    <id>http://arxiv.org/abs/2301.12345v2</id>
    <updated>2023-02-01T10:00:00Z</updated>
    <published>2023-01-30T10:00:00Z</published>
    <title>  Dreamer: World   Models
      for Reinforcement Learning </title>
    <summary>  We present Dreamer, an agent that learns a world model.  </summary>
    <author><name>Danijar Hafner</name></author>
    <author><name>Timothy Lillicrap</name></author>
    <link rel="alternate" href="http://arxiv.org/abs/2301.12345v2" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2301.12345v2" type="application/pdf"/>
    <arxiv:primary_category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
    <arxiv:comment>12 pages</arxiv:comment>
    <arxiv:doi>10.48550/arXiv.2301.12345</arxiv:doi>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2302.99999v1</id>
    <updated>2023-03-01T10:00:00Z</updated>
    <published>2023-02-28T10:00:00Z</published>
    <title>Second paper</title>
    <summary>short abstract</summary>
    <author><name>Jane Doe</name></author>
    <link rel="alternate" href="http://arxiv.org/abs/2302.99999v1" type="text/html"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>
"""


def _ctx():
    return ToolContext(request_id="t", config={}, budget=None)


class _Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", "https://export.arxiv.org/api/query")
            raise httpx.HTTPStatusError(
                "err", request=req,
                response=httpx.Response(self.status_code, text=self.text, request=req))


class _FakeClient:
    """Drop-in for httpx.AsyncClient: records get() calls, serves a canned feed."""
    calls = []
    response = _Resp(FEED)
    raise_exc = None                       # set to an exception to simulate a dead network

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, params=None):
        type(self).calls.append({"url": url, "params": params})
        if type(self).raise_exc is not None:
            raise type(self).raise_exc
        return type(self).response


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.response = _Resp(FEED)
    _FakeClient.raise_exc = None
    monkeypatch.setattr(M.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _run(tool, args):
    return asyncio.run(tool.execute(args, _ctx()))


def test_search_builds_query_and_parses_entries(fake_http):
    r = _run(ArxivSearch(), {"query": "ti:JEPA", "max_results": 5})
    call = fake_http.calls[0]
    assert call["url"] == "https://export.arxiv.org/api/query"
    assert call["params"] == {
        "search_query": "ti:JEPA", "start": 0, "max_results": 5,
        "sortBy": "relevance", "sortOrder": "descending",
    }
    assert r.status == "ok" and r.result["count"] == 2
    p = r.result["papers"][0]
    assert p["id"] == "2301.12345v2"                        # /abs/ suffix stripped
    assert p["title"] == "Dreamer: World Models for Reinforcement Learning"  # ws-collapsed
    assert p["authors"] == ["Danijar Hafner", "Timothy Lillicrap"]
    assert p["published"] == "2023-01-30" and p["updated"] == "2023-02-01"
    assert p["primary_category"] == "cs.LG"
    assert p["categories"] == ["cs.LG", "cs.AI"]
    assert p["pdf_url"] == "http://arxiv.org/pdf/2301.12345v2"
    assert p["abs_url"] == "http://arxiv.org/abs/2301.12345v2"
    assert p["comment"] == "12 pages" and p["doi"] == "10.48550/arXiv.2301.12345"
    # entry without primary_category falls back to the first category term
    assert r.result["papers"][1]["primary_category"] == "cs.CL"


def test_max_results_clamped_to_50(fake_http):
    r = _run(ArxivSearch(), {"query": "cat:cs.LG", "max_results": 999})
    assert r.status == "ok"
    assert fake_http.calls[0]["params"]["max_results"] == 50


def test_sort_args_passed_through(fake_http):
    _run(ArxivSearch(), {"query": "x", "sort_by": "submittedDate",
                         "sort_order": "ascending"})
    params = fake_http.calls[0]["params"]
    assert params["sortBy"] == "submittedDate"
    assert params["sortOrder"] == "ascending"


def test_long_abstracts_trimmed(fake_http):
    fake_http.response = _Resp(FEED.replace(
        "We present Dreamer, an agent that learns a world model.", "x" * 600))
    r = _run(ArxivSearch(), {"query": "x"})
    p = r.result["papers"][0]
    assert len(p["summary"]) == 501 and p["summary"].endswith("…")


def test_http_status_error_is_caught(fake_http):
    fake_http.response = _Resp("service unavailable", status=503)
    r = _run(ArxivSearch(), {"query": "x"})
    assert r.status == "error" and r.error.startswith("HTTP 503")
    assert "service unavailable" in r.error


def test_network_error_is_caught(fake_http):
    fake_http.raise_exc = httpx.ConnectError("no route")
    r = _run(ArxivSearch(), {"query": "x"})
    assert r.status == "error" and "ConnectError" in r.error


def test_malformed_xml_is_caught(fake_http):
    fake_http.response = _Resp("this is not <xml")
    r = _run(ArxivGet(), {"ids": ["2301.12345"]})
    assert r.status == "error" and "ParseError" in r.error


def test_get_joins_ids_and_rejects_empty(fake_http):
    r = _run(ArxivGet(), {"ids": ["2301.12345", "2302.99999v1"]})
    assert r.status == "ok" and r.result["count"] == 2
    params = fake_http.calls[0]["params"]
    assert params["id_list"] == "2301.12345,2302.99999v1"
    assert params["max_results"] == 2
    # empty id list never reaches the network
    assert _run(ArxivGet(), {"ids": []}).status == "error"
    assert len(fake_http.calls) == 1
