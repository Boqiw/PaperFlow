"""Zotero 端：每周一个「第n周」标签，而且失败不许把整周搞崩。"""

from __future__ import annotations

import pytest
import requests

from paperflow_core import zotero


@pytest.fixture
def stub_pdfs(monkeypatch):
    """把"挂 PDF"那一步换成"全都挂上了"。

    这些用例只关心建条目那次请求。真去跑会去下载 PDF——测试必须不出网。
    """
    monkeypatch.setattr(
        zotero, "attach_pdfs", lambda base, session, papers: {"attached": len(papers), "missing": 0}
    )


def test_item_carries_exactly_one_week_tag(paper):
    """标签只留「第n周」，方便在 Zotero 里筛出本周该读的东西。"""
    item = zotero.to_zotero_item(paper(), 3)
    assert item["tags"] == [{"tag": "第3周"}]


def test_item_tag_has_no_zero_padding(paper):
    assert zotero.to_zotero_item(paper(), 1)["tags"][0]["tag"] == "第1周"
    assert zotero.to_zotero_item(paper(), 12)["tags"][0]["tag"] == "第12周"


def test_item_keeps_the_metadata_zotero_needs(paper):
    item = zotero.to_zotero_item(paper(), 2)
    assert item["itemType"] == "journalArticle"
    assert item["title"] == "Metastability in resting-state brain dynamics"
    assert item["DOI"] == "10.1000/xyz"
    assert item["publicationTitle"] == "NeuroImage"
    assert item["date"] == "2023"
    assert item["creators"][0] == {"creatorType": "author", "lastName": "Author", "firstName": "A."}


def test_item_extra_records_the_week_and_rank(paper):
    item = zotero.to_zotero_item(paper(), 4, rank=2, role="deep")
    assert "PaperFlow" in item["extra"]
    assert "第4周" in item["extra"]
    assert "Role: deep" in item["extra"]
    assert "Rank: 2" in item["extra"]


def test_item_accepts_plain_string_authors(paper):
    """手写的种子 JSON 里作者可能就是光秃秃的字符串，不该炸。"""
    item = zotero.to_zotero_item(paper(authors=["Ada Lovelace"]), 1)
    assert item["creators"] == [{"creatorType": "author", "lastName": "Lovelace", "firstName": "Ada"}]


def test_item_falls_back_to_the_pdf_url(paper):
    item = zotero.to_zotero_item(paper(url="", open_pdf="https://example.org/a.pdf"), 1)
    assert item["url"] == "https://example.org/a.pdf"


def test_split_name_handles_both_orders_and_single_names():
    assert zotero.split_name("Ada Lovelace") == {"firstName": "Ada", "lastName": "Lovelace"}
    assert zotero.split_name("Lovelace, Ada") == {"firstName": "Ada", "lastName": "Lovelace"}
    assert zotero.split_name("Plato") == {"firstName": "", "lastName": "Plato"}


def test_push_with_no_papers_is_a_no_op():
    result = zotero.push_papers([], 1)
    assert result["ok"] is True
    assert result["count"] == 0


def test_connector_mode_needs_no_credentials(monkeypatch):
    """ZOTERO_LOCAL=1 走本机连接器接口，不要 user id 也不要 key。

    回归防护：ZOTERO_LOCAL=1 以前被接到只读的 ``/api`` 上，于是「开了这个开关
    也照样推不进去」。现在它指的是连接器接口，唯一前提是 Zotero 开着。
    """
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_API_URL", raising=False)
    monkeypatch.setenv("ZOTERO_LOCAL", "1")

    assert zotero.use_connector() is True
    assert zotero.connector_base() == zotero.CONNECTOR_API
    assert zotero.config_error("zh") is None


def test_the_connector_endpoint_is_not_the_readonly_api():
    """别把连接器接口和那个只读的 /api 搞混——写能力只在连接器这边。"""
    assert zotero.CONNECTOR_API.endswith("/connector")
    assert "/api" not in zotero.CONNECTOR_API


def test_connector_items_use_our_own_keys_and_plain_tags(paper):
    """``id`` 必须是我们自己编的：连接器靠它把"建条目"和"挂 PDF"两次请求对起来。

    saveItems 成功时正文是空的，没有任何东西能反查条目 key，所以这个键只能由
    调用方给。标签用裸字符串（不是 Web API 那种 ``{"tag": ...}``），ItemSaver 认这个。
    """
    items = zotero.to_connector_items([paper(), paper(paperId="s2:2")], 4)

    assert [item["id"] for item in items] == ["pf01", "pf02"]
    assert all(item["tags"] == ["第4周"] for item in items)
    assert all(item["itemType"] == "journalArticle" for item in items)


def test_connector_items_keep_the_same_fields_as_the_web_api(paper):
    """两条写入路径必须落在同一批字段上，否则连接器这条路会悄悄丢信息。"""
    paper = paper(role="deep")
    connector_item = zotero.to_connector_items([paper], 4)[0]
    api_item = zotero.to_zotero_item(paper, 4, rank=1, role="deep")

    for field in ("title", "creators", "publicationTitle", "date", "DOI", "url", "abstractNote", "extra"):
        assert connector_item[field] == api_item[field], field
    assert connector_item["extra"].splitlines()[0] == "PaperFlow"
    assert "第4周" in connector_item["extra"]
    assert "Role: deep" in connector_item["extra"]
    assert "Rank: 1" in connector_item["extra"]


def test_connector_items_accept_plain_string_authors(paper):
    item = zotero.to_connector_items([paper(authors=["Ada Lovelace", "Plato"])], 1)[0]

    assert item["creators"] == [
        {"creatorType": "author", "lastName": "Lovelace", "firstName": "Ada"},
        {"creatorType": "author", "lastName": "Plato", "firstName": ""},
    ]


def test_pdf_url_prefers_the_open_pdf_from_the_search_result(paper):
    assert zotero.pdf_url(paper()) == "https://example.org/paper.pdf"


def test_pdf_url_builds_the_arxiv_download_link(paper):
    """arXiv 条目常常没有 open_pdf，但下载地址是可拼的，拼比不拼强。"""
    assert zotero.pdf_url(paper(paperId="arxiv:2405.15239", open_pdf="")) == (
        "https://arxiv.org/pdf/2405.15239"
    )


def test_pdf_url_gives_up_instead_of_guessing_a_publisher_link(paper):
    """有 DOI 但不代表 ``https://doi.org/...`` 就是 PDF——那是落地页，不猜。"""
    assert zotero.pdf_url(paper(paperId="pubmed:123", doi="10.1/x", open_pdf="")) == ""


def test_download_pdf_rejects_an_html_landing_page(monkeypatch):
    """出版社对不认识的客户端回登录页，那种 HTML 存进去只会变成一条点开报错的坏附件。"""
    monkeypatch.setattr(zotero, "request", lambda *a, **k: b"<html>please log in</html>")
    assert zotero.download_pdf("https://example.org/x.pdf") is None


def test_download_pdf_returns_the_bytes_of_a_real_pdf(monkeypatch):
    monkeypatch.setattr(zotero, "request", lambda *a, **k: b"%PDF-1.4\nbody")
    assert zotero.download_pdf("https://example.org/x.pdf") == b"%PDF-1.4\nbody"


def test_download_pdf_refuses_an_oversized_file(monkeypatch):
    """没有大小上限的话，一条假链接就能把一次 run 拖死。"""
    big = b"%PDF-" + b"0" * (zotero.MAX_PDF_BYTES + 1)
    monkeypatch.setattr(zotero, "request", lambda *a, **k: big)
    assert zotero.download_pdf("https://example.org/x.pdf") is None


def test_download_pdf_never_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.Timeout("too slow")

    monkeypatch.setattr(zotero, "request", boom)
    assert zotero.download_pdf("https://example.org/x.pdf") is None
    assert zotero.download_pdf("") is None


def test_download_pdf_gives_a_dead_link_one_attempt_only(monkeypatch):
    """回归防护：一条死链不该被退避重试 5 次。

    以前那个组合（120 秒超时 × 5 次尝试）意味着**一个** PDF 就能白等十分钟，
    一周十几篇里只要几篇链接是坏的，这次 ``week`` 就废了。
    """
    sent = []

    def fake_request(method, url, **kwargs):
        sent.append(kwargs)
        raise requests.Timeout("too slow")

    monkeypatch.setattr(zotero, "request", fake_request)

    assert zotero.download_pdf("https://example.org/x.pdf") is None
    assert len(sent) == 1  # 只试一次
    assert sent[0]["timeout"] == zotero.PDF_TIMEOUT
    assert sent[0]["retries"] == 1


def test_connector_calls_use_a_short_timeout_and_no_retries(monkeypatch, paper):
    """连接器是本机服务：几秒不回就是没在工作，不该按"互联网抖动"重试。

    回归防护：这两个请求（问分类 + 建条目）以前各自能等 4~5 分钟，
    加起来让一次 ``week`` 卡住十几分钟。
    """
    monkeypatch.setenv("ZOTERO_LOCAL", "1")
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    sent = []

    def fake_post(url, payload, headers=None, params=None, **kwargs):
        sent.append(kwargs)
        return {"name": "我的文库"}

    def fake_post_json(url, payload, headers=None, params=None, **kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(zotero, "api_post", fake_post)
    monkeypatch.setattr(zotero, "api_post_json", fake_post_json)
    monkeypatch.setattr(zotero, "attach_pdfs", lambda base, session, papers: {"attached": 0, "missing": len(papers)})

    assert zotero.push_papers([paper()], 1)["ok"] is True

    assert len(sent) == 2  # getSelectedCollection + saveItems
    for kwargs in sent:
        assert kwargs["timeout"] == zotero.CONNECTOR_TIMEOUT
        assert kwargs["retries"] == zotero.CONNECTOR_RETRIES


def test_attach_pdfs_uploads_one_pdf_per_item(monkeypatch, paper):
    """挂 PDF 是逐条一次上传，每次都要指回正确的连接器键。"""
    keys = []
    monkeypatch.setattr(zotero, "download_pdf", lambda url: b"%PDF-1.4")
    monkeypatch.setattr(zotero, "upload_pdf", lambda base, session, key, data, url: keys.append(key) or True)

    result = zotero.attach_pdfs(
        "http://localhost:23119/connector", "sess", [paper(), paper(paperId="s2:2")]
    )

    assert result == {"attached": 2, "missing": 0}
    assert keys == ["pf01", "pf02"]


def test_attach_pdfs_falls_back_to_zoteros_own_resolver(monkeypatch, paper):
    """我们抓不到（没有 open_pdf、或者链接已死）时，让 Zotero 自己去找开放获取副本。"""
    monkeypatch.setattr(zotero, "download_pdf", lambda url: None)
    monkeypatch.setattr(zotero, "upload_pdf", lambda *a: pytest.fail("不该走到上传"))
    monkeypatch.setattr(zotero, "attach_from_resolver", lambda base, session, key: True)

    result = zotero.attach_pdfs("http://localhost:23119/connector", "sess", [paper()])

    assert result == {"attached": 1, "missing": 0}


def test_attach_pdfs_counts_what_it_could_not_get(monkeypatch, paper):
    """闭源论文就是拿不到 PDF，这必须只是"少一条"，绝不能把整次推送判成失败。"""
    monkeypatch.setattr(zotero, "download_pdf", lambda url: None)
    monkeypatch.setattr(zotero, "attach_from_resolver", lambda base, session, key: False)

    result = zotero.attach_pdfs(
        "http://localhost:23119/connector", "sess", [paper(), paper(paperId="s2:2")]
    )

    assert result == {"attached": 0, "missing": 2}


def test_upload_pdf_points_the_attachment_at_our_item_key(monkeypatch):
    import json

    sent = {}

    def fake_post_bytes(url, data, headers=None, parse="text", **kwargs):
        sent["url"] = url
        sent["data"] = data
        sent["metadata"] = json.loads((headers or {})["X-Metadata"])
        sent["kwargs"] = kwargs

    monkeypatch.setattr(zotero, "api_post_bytes", fake_post_bytes)

    ok = zotero.upload_pdf(
        "http://localhost:23119/connector", "sess-1", "pf02", b"%PDF-1.4", "https://example.org/a.pdf"
    )

    assert ok is True
    assert sent["url"] == "http://localhost:23119/connector/saveAttachment?sessionID=sess-1"
    assert sent["data"] == b"%PDF-1.4"
    assert sent["metadata"]["sessionID"] == "sess-1"
    assert sent["metadata"]["parentItemID"] == "pf02"  # 不指回条目就成了无主附件
    assert sent["metadata"]["url"] == "https://example.org/a.pdf"
    # 上传也是在本机连接器上，超时同样得收紧
    assert sent["kwargs"]["timeout"] == zotero.CONNECTOR_TIMEOUT
    assert sent["kwargs"]["retries"] == zotero.CONNECTOR_RETRIES


def test_upload_pdf_reports_failure_instead_of_raising(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.HTTPError("400 Bad Request")

    monkeypatch.setattr(zotero, "api_post_bytes", boom)
    assert zotero.upload_pdf("http://localhost:23119/connector", "s", "pf01", b"%PDF-", "u") is False


def test_resolver_lookup_disables_retries(monkeypatch):
    """连接器用它自己的 500 表达"这篇没有开放获取副本"——那是业务结果，不是抖动。

    默认逻辑会把 5xx 退避重试，于是每篇闭源论文白等半分钟，还能攒出几十秒的无谓等待。
    """
    sent = {}

    def fake_post_json(url, payload, headers=None, params=None, parse="json", retry_status=None, **kwargs):
        sent["url"] = url
        sent["payload"] = payload
        sent["parse"] = parse
        sent["retry_status"] = retry_status
        sent["kwargs"] = kwargs

    monkeypatch.setattr(zotero, "api_post_json", fake_post_json)

    assert zotero.attach_from_resolver("http://localhost:23119/connector", "sess-2", "pf03") is True
    assert sent["url"].endswith("/connector/saveAttachmentFromResolver")
    assert sent["payload"] == {"sessionID": "sess-2", "itemID": "pf03"}
    assert sent["parse"] == "text"
    assert sent["retry_status"] == ()
    assert sent["kwargs"]["timeout"] == zotero.CONNECTOR_TIMEOUT
    assert sent["kwargs"]["retries"] == zotero.CONNECTOR_RETRIES


def test_save_items_reads_the_response_as_text(monkeypatch, paper):
    """saveItems 成功时正文是空的；按 JSON 解析会直接抛 JSONDecodeError。"""
    sent = {}

    def fake_post_json(url, payload, headers=None, params=None, parse="json", retry_status=None, **kwargs):
        sent["url"] = url
        sent["payload"] = payload
        sent["parse"] = parse

    monkeypatch.setattr(zotero, "api_post_json", fake_post_json)

    zotero.connector_save_items("http://localhost:23119/connector", "sess-3", [paper()], 6)

    assert sent["url"] == "http://localhost:23119/connector/saveItems"
    assert sent["payload"]["sessionID"] == "sess-3"
    assert sent["payload"]["items"][0]["id"] == "pf01"
    assert sent["parse"] == "text"


def test_connector_push_saves_items_and_names_the_collection(stub_pdfs, monkeypatch, paper):
    """分类名必须报出来：条目进哪个分类由 Zotero 的当前选中项决定，是个隐形规则。"""
    monkeypatch.setenv("ZOTERO_LOCAL", "1")
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    sent = {}

    def fake_post(url, payload, headers=None, params=None, **kwargs):
        sent["target_url"] = url
        return {"name": "MyCollection", "libraryName": "我的文库", "editable": True}

    def fake_post_json(url, payload, headers=None, params=None, parse="json", retry_status=None, **kwargs):
        sent["save_url"] = url
        sent["payload"] = payload
        sent["headers"] = headers or {}

    monkeypatch.setattr(zotero, "api_post", fake_post)
    monkeypatch.setattr(zotero, "api_post_json", fake_post_json)

    result = zotero.push_papers([paper(), paper(paperId="s2:2")], 4)

    assert result["ok"] is True
    assert result["count"] == 2
    assert sent["target_url"].endswith("/connector/getSelectedCollection")
    assert sent["save_url"] == "http://localhost:23119/connector/saveItems"
    assert [item["id"] for item in sent["payload"]["items"]] == ["pf01", "pf02"]
    assert all(item["tags"] == ["第4周"] for item in sent["payload"]["items"])
    assert "MyCollection" in result["message"]
    # Zotero 会把 UA 以 Mozilla/ 开头的请求当浏览器掐掉，这个头不能少
    assert sent["headers"]["X-Zotero-Connector-API-Version"] == "3"


def test_connector_push_reports_how_many_pdfs_landed(monkeypatch, paper):
    """用户要的是"每条都带 PDF"，所以没有 PDF 的条数必须明说，不能吞掉。"""
    monkeypatch.setenv("ZOTERO_LOCAL", "1")
    monkeypatch.setattr(zotero, "api_post", lambda url, payload, headers=None, params=None, **kwargs: {"name": "文库"})
    monkeypatch.setattr(zotero, "api_post_json", lambda *a, **k: None)
    monkeypatch.setattr(zotero, "attach_pdfs", lambda base, session, papers: {"attached": 1, "missing": 1})

    result = zotero.push_papers([paper(), paper(paperId="s2:2")], 7)

    assert result["ok"] is True
    assert result["count"] == 2
    assert "1 条挂上了 PDF" in result["message"]
    assert "另有 1 条没找到开放获取的 PDF" in result["message"]


def test_every_connector_save_uses_a_fresh_session_id(stub_pdfs, monkeypatch, paper):
    """回归防护：复用同一个会话 ID 会被 Zotero **永久**拒绝。

    它的 SessionManager.create() 发现 ID 已存在就抛错（回 409 SESSION_EXISTS），
    而不带 session 参数时拿到的 ID 是 null —— 第二次保存必然撞车；更糟的是它回收
    旧会话的 gc() 写错了变量名会抛 TypeError，旧会话永不清理，所以不是"等一下就好"。
    只有每次都换新 ID 才能稳定写进去。
    """
    monkeypatch.setenv("ZOTERO_LOCAL", "1")
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    sessions = []

    monkeypatch.setattr(zotero, "api_post", lambda url, payload, headers=None, params=None, **kwargs: {"name": "我的文库"})

    def fake_post_json(url, payload, headers=None, params=None, parse="json", retry_status=None, **kwargs):
        sessions.append(payload.get("sessionID"))

    monkeypatch.setattr(zotero, "api_post_json", fake_post_json)

    assert zotero.push_papers([paper()], 1)["ok"] is True
    assert zotero.push_papers([paper(paperId="s2:2")], 2)["ok"] is True

    assert len(sessions) == 2
    assert all(sessions)
    assert sessions[0] != sessions[1]


def test_connector_push_uses_json_headers_for_the_target_lookup(stub_pdfs, monkeypatch, paper):
    """getSelectedCollection 收 JSON; 正文要是 text/plain 它读不到分类。"""
    monkeypatch.setenv("ZOTERO_LOCAL", "1")
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    seen = {}

    def fake_post(url, payload, headers=None, params=None, **kwargs):
        seen["content_type"] = (headers or {}).get("Content-Type")
        return {"name": "我的文库"}

    monkeypatch.setattr(zotero, "api_post", fake_post)
    monkeypatch.setattr(zotero, "api_post_json", lambda *a, **k: None)

    zotero.push_papers([paper()], 1)
    assert seen["content_type"] == "application/json"


def test_connector_push_says_zotero_is_not_running(monkeypatch, paper):
    """Zotero 没开是这条路最常见的失败，得说人话，而不是丢一个 ConnectionError。"""
    monkeypatch.setenv("ZOTERO_LOCAL", "1")
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)

    def refused(*args, **kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(zotero, "api_post", refused)
    monkeypatch.setattr(zotero, "api_post_json", refused)

    result = zotero.push_papers([paper()], 2)

    assert result["ok"] is False
    assert result["count"] == 0
    assert "连不上本机 Zotero" in result["message"]
    assert "第2周" in result["message"]


def test_connector_push_reports_an_import_failure_instead_of_raising(stub_pdfs, monkeypatch, paper):
    monkeypatch.setenv("ZOTERO_LOCAL", "1")
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.setattr(zotero, "api_post", lambda url, payload, headers=None, params=None, **kwargs: {"name": "我的文库"})

    def boom(*args, **kwargs):
        raise requests.HTTPError("400 Bad Request | 没有可用的导入翻译器")

    monkeypatch.setattr(zotero, "api_post_json", boom)
    result = zotero.push_papers([paper()], 1)

    assert result["ok"] is False
    assert "Zotero 导入失败" in result["message"]
    assert "阅读清单" in result["message"]  # 明确告诉用户清单还在


def test_connector_push_survives_not_knowing_the_collection(stub_pdfs, monkeypatch, paper):
    """问不到分类名只是少了条信息，不该因此把推送整个放弃。"""
    monkeypatch.setenv("ZOTERO_LOCAL", "1")
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)

    def boom(url, payload, headers=None, params=None, **kwargs):
        raise requests.HTTPError("500 Server Error")

    monkeypatch.setattr(zotero, "api_post", boom)
    monkeypatch.setattr(zotero, "api_post_json", lambda *a, **k: None)

    result = zotero.push_papers([paper()], 1)

    assert result["ok"] is True
    assert result["count"] == 1
    assert "当前选中的分类" in result["message"]


def test_a_custom_remote_api_url_still_requires_credentials(monkeypatch, paper):
    """``ZOTERO_API_URL`` 不发放通行证：它只是换个地址，凭据该要还是要。"""
    monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
    monkeypatch.setenv("ZOTERO_API_URL", "https://zotero.example.org/api")
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)

    assert zotero.config_error("zh")
    assert zotero.push_papers([paper()], 1)["ok"] is False


def test_push_without_credentials_returns_not_ok_instead_of_raising(monkeypatch, paper):
    """没配 key 是最常见的情况，必须返回结构化结果，不能抛异常。"""
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
    monkeypatch.delenv("ZOTERO_API_URL", raising=False)

    result = zotero.push_papers([paper()], 1)
    assert result["ok"] is False
    assert result["count"] == 0
    assert result["message"]


def test_push_reports_a_network_failure_instead_of_raising(monkeypatch, paper):
    monkeypatch.setenv("ZOTERO_USER_ID", "12345")
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    monkeypatch.delenv("ZOTERO_LOCAL", raising=False)

    def boom(*args, **kwargs):
        raise ConnectionResetError("connection reset by peer")

    monkeypatch.setattr(zotero, "zotero_create", boom)
    result = zotero.push_papers([paper()], 2)
    assert result["ok"] is False
    assert "阅读清单" in result["message"]  # 明确告诉用户清单还在
    assert "第2周" in result["message"]  # 提醒标签会丢，建议手动导入


def test_push_uses_the_week_label_for_the_item_tags(monkeypatch, paper):
    monkeypatch.setenv("ZOTERO_USER_ID", "12345")
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
    captured = {}

    def fake_create(items, user_id, api_key=None):
        captured["items"] = items
        # Zotero 是批量写入，successful 里每个键对应一条真正进去的记录——
        # push_papers 报的就是这个数，所以假实现必须把两条都算成功。
        return {"successful": {"0": {"key": "AAAA"}, "1": {"key": "BBBB"}}, "failed": {}}

    monkeypatch.setattr(zotero, "zotero_create", fake_create)
    result = zotero.push_papers(
        [paper(paperId="s2:1", doi="10.1000/one"), paper(paperId="s2:2", doi="10.1000/two")], 5
    )

    assert result["ok"] is True
    assert result["count"] == 2
    assert "第5周" in result["message"]
    assert all(item["tags"] == [{"tag": "第5周"}] for item in captured["items"])


def test_push_flags_rejected_items(monkeypatch, paper):
    monkeypatch.setenv("ZOTERO_USER_ID", "12345")
    monkeypatch.setenv("ZOTERO_API_KEY", "secret")
    monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
    monkeypatch.setattr(
        zotero,
        "zotero_create",
        lambda items, user_id, api_key=None: {"successful": {}, "failed": {"0": {"message": "nope"}}},
    )
    result = zotero.push_papers([paper()], 1)
    assert result["ok"] is False
    assert "被 Zotero 拒绝" in result["message"]


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_failure_messages_are_localised(monkeypatch, paper, lang):
    monkeypatch.delenv("ZOTERO_USER_ID", raising=False)
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.delenv("ZOTERO_LOCAL", raising=False)
    monkeypatch.delenv("ZOTERO_API_URL", raising=False)
    assert zotero.push_papers([paper()], 1, lang=lang)["message"]
