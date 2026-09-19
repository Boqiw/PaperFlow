"""Zotero 导入：把这一周的清单推进去，只打一个标签 ``第n周``。

两条写入路径（用 ``ZOTERO_LOCAL`` 选）
--------------------------------------

1. **连接器（``ZOTERO_LOCAL=1``，零配置，推荐）**——复用浏览器插件那条通道
   ``POST http://localhost:23119/connector/saveItems``。不需要任何 key，但**推送时
   Zotero 必须开着**；而且条目会落进**你当前选中的分类**（这条规则写死在 Zotero 的
   ``getSaveTarget()`` 里，请求方无法指定），所以推送前会先问一次分类名，报给你看。
   条目字段和 Web API 那条路一样齐（标题/作者/期刊/年份/DOI/摘要/``extra``/标签），
   然后逐条把 PDF 全文挂上去。
2. **Web API**——配 ``ZOTERO_USER_ID`` + ``ZOTERO_API_KEY``。唯一能指定分类、
   而且 Zotero 不用开着的一条路。

连接器这条路怎么挂 PDF
----------------------

``saveItems`` 每个条目有个 ``id`` 字段，**由调用方自己编**，Zotero 拿它当"连接器键"
存进这一次的保存会话；后面 ``saveAttachment`` 就靠 ``X-Metadata.parentItemID`` 指回它。
所以流程是：**先建条目（键是我们给的），再逐条挂 PDF**，而且两次请求的会话 ID 必须一致。

为什么不先用 ``/connector/import``（喂 RIS）再挂附件：那个端点回的是 Zotero 条目对象，
里面**没有**能当连接器键的字段，于是 ``saveAttachment`` 拿不到 ``parentItemID``，
一律 ``500``；而且 RIS 没有能写 ``extra`` 的字段（``N1`` 会变成子笔记）。
现在统一走 ``saveItems``，一条路径同时解决这两件事。

PDF 从哪来：优先用搜索结果自带的 ``open_pdf``（出版社/仓储给的开放获取直链）；
arXiv 条目按 ID 拼 ``https://arxiv.org/pdf/<id>``；**两条都没有、或者抓下来的头四字节
不是 ``%PDF``**（防着把 HTML 落地页当 PDF 存进去）时，再让 Zotero 自己按 DOI / PMID / 
arXiv 找开放获取副本（``saveAttachmentFromResolver``）。这个兜底**每个条目只能调一次**
——多调一次它就再挂一份重复附件。

**别再用 ``http://localhost:23119/api``（那个"本地只读 API"）**：Zotero 9.0.6 里它的
全部 21 个端点都是 ``supportedMethods = ['GET']``，纯只读，POST 一律回
``400 Endpoint does not support method``；写能力只存在于上游尚未发布的分支。
而且它**默认是关的**（``GET /api/users/0/items`` 回 ``403 Local API is not enabled``，
要在设置里手动打开），所以它既写不进去、也拿不到来核对结果。
``ZOTERO_LOCAL=1`` 以前被接到这条路上，于是永远推不进去——而那个 400 的响应体
又被 HTTP 层吞掉了，白查了很久。

设计取舍（都是踩过之后定下来的）
--------------------------------

* **只打一个标签**。以前打过 ``queue-date-*`` / ``status-*`` / ``concept-*``，
  标签体系一膨胀就没法在 Zotero 侧边栏里用了。现在打开标签面板，
  看到的就是 ``第1周``、``第2周``……点哪个读哪个。
* **不去读 Zotero**。写入接口不提供按 DOI 去重，所以去重交给账本
  （:mod:`paperflow_core.ledger`），副作用是你不用给任何额外权限。
* **绝不因为写不进去就让整周空白**。所有失败都转成返回值里的
  ``ok=False``，由调用方记进周报。调用方那边的顺序也为这条服务：
  **本地产出（账本、清单、周报）全部先落盘，最后才调用这里**，所以 Zotero 超时
  影响不到它们是不是写得出来。
* **本机连接器只等几十秒**。连接器调用的超时是 :data:`CONNECTOR_TIMEOUT`、并且不重试
  （:data:`CONNECTOR_RETRIES`）。它是本地服务，半分钟不回就是没在工作；
  默认的"60 秒 × 5 次退避"曾经让一次 ``week`` 在本机连接器上白等十几分钟。
* **每次导入都用新的会话 ID**。连接器要求请求方自己给一个"保存会话"的编号，重复使用
  同一个编号会被它永久拒绝（见 :func:`push_via_connector` 的说明），所以这里用 UUID。
  （同一个 UUID 内部要连着用两次：建条目一次、挂 PDF 一次——那才是同一次会话。）
* **PDF 拿不到不算失败**。挂不上附件只是少一份全文，条目本身（含 DOI 和链接）已经
  进去了，所以抓取与上传全程不抛异常，只在最后回报"几条没挂上"。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

import requests

from .config import user_agent
from .http_client import api_post, api_post_bytes, api_post_json, request
from .texts import pick
from .vault import week_label

WEB_API = "https://api.zotero.org"
CONNECTOR_API = "http://localhost:23119/connector"

#: 单个 PDF 的大小上限。有的"PDF 链接"其实指向几百 MB 的文件，或者是个不会结束的流，
#: 没有上限就能把一次 run 拖死。
MAX_PDF_BYTES = 60 * 1024 * 1024

#: 抓 PDF 的超时（秒）。比接口调用宽松：出版社的下载口经常慢得离谱。
#: 但也不能太宽——一条死链等两分钟、再乘上 5 次重试，就是一个 PDF 十分钟。
PDF_TIMEOUT = 45

#: 连接器（本机 Zotero）调用的超时（秒）与尝试次数。它跑在 ``localhost`` 上，
#: 几十秒不回就说明它没在工作（没开、端口不对、主线程被别的操作占着）；
#: 默认的"60 秒 × 5 次退避"能把它一次卡成五分半钟，两个调用加起来十几分钟。
#: 实测：正常的连接器 2 秒内就回话了，而一次 12 条批量建条目需要几秒到十几秒，
#: 所以留 30 秒——够用，又不会把一次 ``week`` 拖住。
#: **不重试**是故意的：``saveItems`` 是个写入请求，超时后重发可能让 Zotero 存进两份。
CONNECTOR_TIMEOUT = 30
CONNECTOR_RETRIES = 1


def use_connector() -> bool:
    """``ZOTERO_LOCAL=1`` → 走本机 Zotero 的连接器接口。"""
    return (os.getenv("ZOTERO_LOCAL") or "").strip().lower() in {"1", "true", "yes"}


def connector_base() -> str:
    """连接器接口根地址。Zotero 的 HTTP 服务端口能在设置里改，所以留个覆盖口。"""
    return (os.getenv("ZOTERO_CONNECTOR_URL") or CONNECTOR_API).rstrip("/")


def zotero_api_base() -> str:
    """Web API 地址；配了 ``ZOTERO_API_URL`` 就用它（自建服务 / 反向代理）。"""
    explicit = os.getenv("ZOTERO_API_URL")
    return explicit.rstrip("/") if explicit else WEB_API


def zotero_headers() -> Dict[str, str]:
    return {"Zotero-API-Version": "3", "Content-Type": "application/json", "User-Agent": user_agent()}


def connector_headers(content_type: str = "text/plain; charset=utf-8") -> Dict[str, str]:
    """连接器接口的请求头。

    ``X-Zotero-Connector-API-Version`` 不是可有可无的：Zotero 的 ``_processEndpoint``
    会把 User-Agent 以 ``Mozilla/`` 开头（或带 ``Origin``）的请求当成浏览器发来的
    并直接掐断，除非它看到这个头。我们现在的 UA 不以 Mozilla 开头，但没理由留这个坑。
    """
    return {
        "User-Agent": user_agent(),
        "Content-Type": content_type,
        "X-Zotero-Connector-API-Version": "3",
    }


def zotero_create(items: List[Dict[str, Any]], user_id: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """写入条目，返回 Zotero 的响应。"""
    url = f"{zotero_api_base()}/users/{user_id}/items"
    headers = dict(zotero_headers())
    if api_key:
        headers["Zotero-API-Key"] = api_key
    return api_post(url, headers=headers, payload=items)


def split_name(name: str) -> Dict[str, str]:
    """拆成 Zotero 要的 ``firstName`` / ``lastName``。

    两种写法都要认：``Ada Lovelace`` 和 ``Lovelace, Ada``。OpenAlex / PubMed /
    Zotero 导出都会出现，弄反了整条文献的作者就是错的。
    """
    text = " ".join((name or "").split())
    if not text:
        return {"firstName": "", "lastName": ""}
    if "," in text:
        last, _, first = text.partition(",")
        return {"firstName": first.strip(), "lastName": last.strip()}
    bits = text.split()
    if len(bits) == 1:
        return {"firstName": "", "lastName": bits[0]}
    return {"firstName": " ".join(bits[:-1]), "lastName": bits[-1]}


def config_error(lang: str = "zh") -> Optional[str]:
    """配置不全时返回一句给人看的说明，配置齐全返回 ``None``。"""
    # 连接器那条路不读任何凭据，唯一前提是 Zotero 开着——那要真发一个请求才知道，
    # 不在这里卡住。
    if use_connector():
        return None
    if not (os.getenv("ZOTERO_API_KEY") or "").strip():
        return pick(
            lang,
            "没配 ZOTERO_API_KEY，这一周跳过 Zotero 推送。"
            "想推进本机 Zotero 就在 .env 里加 ZOTERO_LOCAL=1（不用申请 key，但推送时 Zotero 要开着）；"
            "想推到网页版文库，再补上 ZOTERO_USER_ID 和 ZOTERO_API_KEY。",
            "ZOTERO_API_KEY not set; skipping Zotero this week. Add ZOTERO_LOCAL=1 to push into your "
            "local Zotero (no key needed, but Zotero must be running), or set ZOTERO_USER_ID + "
            "ZOTERO_API_KEY for the web library.",
        )
    if not (os.getenv("ZOTERO_USER_ID") or "").strip():
        return pick(
            lang,
            "没配 ZOTERO_USER_ID，这一周跳过 Zotero 推送。",
            "ZOTERO_USER_ID not set; skipping Zotero this week.",
        )
    return None


def to_zotero_item(
    paper: Dict[str, Any],
    week: int,
    rank: int = 1,
    role: str = "",
    collection_key: Optional[str] = None,
) -> Dict[str, Any]:
    """把内部论文记录转成 Zotero 条目。标签只有 ``第n周`` 一个。"""
    item = {
        "itemType": "journalArticle",
        "title": paper.get("title", ""),
        "creators": paper_creators(paper),
        "abstractNote": paper.get("abstract", ""),
        "publicationTitle": paper.get("venue", ""),
        "date": str(paper.get("year") or ""),
        "DOI": paper.get("doi", ""),
        "url": paper.get("url") or paper.get("open_pdf") or "",
        "tags": [{"tag": week_label(week)}],
        "extra": paper_extra(paper, week, rank, role),
    }
    if collection_key:
        item["collections"] = [collection_key]
    return item


def paper_creators(paper: Dict[str, Any]) -> List[Dict[str, str]]:
    """论文作者 → Zotero 的 ``creators``。两条写入路径共用。"""
    creators: List[Dict[str, str]] = []
    for author in (paper.get("authors") or [])[:30]:
        # 正常是 {"name": "..."}；手写的种子 JSON 里也可能是光秃秃的字符串。
        name = (author.get("name") if isinstance(author, dict) else author) or ""
        name = str(name).strip()
        if name:
            creators.append({"creatorType": "author", **split_name(name)})
    return creators


def paper_extra(paper: Dict[str, Any], week: int, rank: int, role: str = "") -> str:
    """条目 ``extra`` 里的备忘。两条写入路径共用。

    这是"这条目哪来的"唯一线索，所以写全：来自 PaperFlow、内部 Paper ID（用来和账本对
    得上）、第几周、当时定的角色和名次。
    """
    lines = ["PaperFlow", f"Paper ID: {paper.get('paperId')}", week_label(week)]
    if role:
        lines.append(f"Role: {role}")
    lines.append(f"Rank: {rank}")
    return "\n".join(lines)


def connector_key(rank: int) -> str:
    """连接器键：同一批里唯一即可，所以直接用序号。"""
    return f"pf{rank:02d}"


def to_connector_items(papers: Sequence[Dict[str, Any]], week: int) -> List[Dict[str, Any]]:
    """把一周的推荐转成连接器 ``saveItems`` 要的 JSON。

    ``id`` 是**我们自己编的连接器键**，不是 Zotero 的条目 key：连接器拿它把"建条目"
    和后面"挂 PDF"两次请求对起来（见 :func:`attach_pdfs`），而 ``saveItems`` 成功时
    响应正文是空的，没有任何东西可以反查——键只能由我们生成，且这一批里唯一。

    标签用裸字符串 ``["第1周"]``，不是 Web API 那种 ``[{"tag": ...}]``：连接器走的是
    Zotero 内部的 ItemSaver，实测裸字符串能正确落库。
    """
    tag = week_label(week)
    items: List[Dict[str, Any]] = []
    for rank, paper in enumerate(papers, start=1):
        items.append(
            {
                "id": connector_key(rank),
                "itemType": "journalArticle",
                "title": paper.get("title", ""),
                "creators": paper_creators(paper),
                "abstractNote": paper.get("abstract", ""),
                "publicationTitle": paper.get("venue", ""),
                "date": str(paper.get("year") or ""),
                "DOI": paper.get("doi", ""),
                "url": paper.get("url") or paper.get("open_pdf") or "",
                "tags": [tag],
                "extra": paper_extra(paper, week, rank, paper.get("role") or ""),
            }
        )
    return items


def pdf_url(paper: Dict[str, Any]) -> str:
    """猜这篇论文的 PDF 直链，猜不到返回空串。

    只做两件有把握的事：用搜索结果自带的 ``open_pdf``；arXiv 条目按 ID 拼下载地址
    （``arxiv:2405.15239`` → ``https://arxiv.org/pdf/2405.15239``）。其余的交给
    Zotero 自己去解析 DOI——出版社的链接规律猜不准，硬猜只会抓回一堆落地页。
    """
    open_pdf = (paper.get("open_pdf") or "").strip()
    if open_pdf:
        return open_pdf
    paper_id = str(paper.get("paperId") or "")
    if paper_id.startswith("arxiv:"):
        return f"https://arxiv.org/pdf/{paper_id.split(':', 1)[1].strip()}"
    return ""


def download_pdf(url: str) -> Optional[bytes]:
    """把 PDF 抓下来；抓不到、或者抓到的根本不是 PDF，都返回 ``None``。

    头四字节必须是 ``%PDF``：不少出版商对不认识的客户端返回登录页/验证页，那种 HTML
    存进 Zotero 只会变成一条点开就报错的坏附件，不如不要。
    """
    if not url:
        return None
    try:
        content = request(
            "GET",
            url,
            headers={"User-Agent": user_agent(), "Accept": "application/pdf"},
            timeout=PDF_TIMEOUT,
            retries=1,
            parse="bytes",
        )
    except Exception:
        return None
    if not content or len(content) > MAX_PDF_BYTES or not content.startswith(b"%PDF"):
        return None
    return content


def upload_pdf(base: str, session: str, key: str, data: bytes, url: str) -> bool:
    """把 PDF 二进制挂到 ``key`` 这个条目上。成功返回 ``True``。

    ``X-Metadata`` 里的 ``parentItemID`` 就是 :func:`to_connector_items` 给的那个键，
    ``sessionID`` 必须和建条目时用的一致（``saveAttachment`` 是去**已有**会话里查条目，
    不像 ``saveItems`` 会新建会话）。
    """
    metadata = {"sessionID": session, "parentItemID": key, "title": "Full Text PDF", "url": url}
    try:
        api_post_bytes(
            f"{base}/saveAttachment?sessionID={session}",
            data,
            headers={
                **connector_headers("application/pdf"),
                "X-Metadata": json.dumps(metadata, ensure_ascii=False),
            },
            timeout=CONNECTOR_TIMEOUT,
            retries=CONNECTOR_RETRIES,
        )
    except Exception:
        return False
    return True


def attach_from_resolver(base: str, session: str, key: str) -> bool:
    """让 Zotero 自己按 DOI/PMID/arXiv 找开放获取副本并挂上。

    它找不到时回 ``500``——那是"这篇没有开放获取副本"这个**业务结果**，不是故障，
    所以必须关掉重试，否则每篇闭源论文都要白等半分钟。
    **每个条目只能调一次**：多调一次它就再挂一份重复附件。
    """
    try:
        api_post_json(
            f"{base}/saveAttachmentFromResolver",
            {"sessionID": session, "itemID": key},
            headers=connector_headers("application/json"),
            parse="text",
            retry_status=(),
            timeout=CONNECTOR_TIMEOUT,
            retries=CONNECTOR_RETRIES,
        )
    except Exception:
        return False
    return True


def attach_pdfs(base: str, session: str, papers: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """给刚建好的这批条目逐条挂 PDF。返回 ``{"attached": n, "missing": n}``。

    全程不抛异常：抓不到 PDF 是常态（闭源期刊、出版社拦爬虫），绝不能因此把整次推送
    判成失败——条目本身已经进 Zotero 了。
    """
    attached = 0
    for rank, paper in enumerate(papers, start=1):
        key = connector_key(rank)
        url = pdf_url(paper)
        data = download_pdf(url)
        if data is not None and upload_pdf(base, session, key, data, url):
            attached += 1
            continue
        if attach_from_resolver(base, session, key):
            attached += 1
    return {"attached": attached, "missing": len(papers) - attached}


def connector_save_items(base: str, session: str, papers: Sequence[Dict[str, Any]], week: int) -> None:
    """建条目。失败会抛异常，由 :func:`push_via_connector` 兜住。"""
    api_post_json(
        f"{base}/saveItems",
        {"sessionID": session, "items": to_connector_items(papers, week)},
        headers=connector_headers("application/json"),
        parse="text",
        timeout=CONNECTOR_TIMEOUT,
        retries=CONNECTOR_RETRIES,
    )


def connector_target() -> str:
    """问 Zotero：这一批会落进哪个分类。问不到就返回空串。"""
    data = api_post(
        f"{connector_base()}/getSelectedCollection",
        {},
        headers=connector_headers("application/json"),
        timeout=CONNECTOR_TIMEOUT,
        retries=CONNECTOR_RETRIES,
    )
    if isinstance(data, dict):
        name = data.get("name") or data.get("libraryName") or ""
        return str(name)
    return ""


def push_via_connector(papers: Sequence[Dict[str, Any]], week: int, lang: str = "zh") -> Dict[str, Any]:
    """走本机 Zotero 的连接器接口导入，并逐条挂上 PDF。**不抛异常**。

    会话 ID **必须是新的**，这是踩出来的坑：Zotero 给每次导入开一个"保存会话"，
    ``SessionManager.create()`` 一发现 ID 已存在就直接抛错，连接器于是回
    ``409 {"error":"SESSION_EXISTS"}``；而不带 ``session`` 参数时它拿到的 ID 是
    ``null``——第二次导入必然撞车。更麻烦的是它负责回收旧会话的 ``gc()`` 把
    ``this._sessions`` 写成了 ``this._session``，抛 TypeError、**旧会话永远不删**，
    所以复用同一个 ID 是永久失败，只能重启 Zotero。浏览器插件每次也是新生成一个。

    同一次会话内部要连着用两次：先 ``saveItems`` 建条目（键由我们编，见
    :func:`to_connector_items`），再用同一个 UUID 逐条 ``saveAttachment`` 挂 PDF。
    """
    base = connector_base()
    try:
        target = connector_target()
    except Exception:
        # 拿不到分类名而已，不该因此放弃推送。
        target = ""
    session = uuid4().hex
    try:
        connector_save_items(base, session, papers, week)
    except Exception as exc:  # 网络/Zotero 没开/格式被拒都不该让这一周白跑
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            message = pick(
                lang,
                f"连不上本机 Zotero（{base}）。ZOTERO_LOCAL=1 要求推送时 Zotero 是开着的，"
                f"这一周跳过；这 {len(papers)} 篇没进 Zotero，需要的话手动导入，"
                f"否则不会带「{week_label(week)}」标签。",
                f"Can't reach the local Zotero at {base}. Zotero must be running when ZOTERO_LOCAL=1; "
                f"these {len(papers)} papers didn't make it in, so import them by hand if you want the "
                f"{week_label(week)} tag.",
            )
        else:
            message = pick(
                lang,
                f"Zotero 导入失败（{type(exc).__name__}: {exc}）。"
                f"阅读清单和账本都已写入，但 Zotero 里这次没有条目。",
                f"Zotero import failed ({type(exc).__name__}: {exc}). The reading list and ledger were "
                f"still written, but Zotero has nothing this week.",
            )
        return {"ok": False, "count": 0, "message": message}
    pdf = attach_pdfs(base, session, papers)
    count = len(papers)
    where = (
        pick(lang, f"分类「{target}」", f'collection "{target}"')
        if target
        else pick(lang, "当前选中的分类", "the selected collection")
    )
    message = pick(
        lang,
        f"已导入 Zotero {count} 条，落在{where}，标签 {week_label(week)}；"
        f"其中 {pdf['attached']} 条挂上了 PDF 全文。",
        f"Imported {count} items into Zotero ({where}) tagged {week_label(week)}; "
        f"{pdf['attached']} of them have the full-text PDF attached.",
    )
    if pdf["missing"]:
        message += pick(
            lang,
            f"另有 {pdf['missing']} 条没找到开放获取的 PDF，需要的话手动拖进去。",
            f" {pdf['missing']} had no open-access PDF; drag them in by hand if you need them.",
        )
    return {"ok": True, "count": count, "message": message}


def push_papers(
    papers: Sequence[Dict[str, Any]],
    week: int,
    collection_key: Optional[str] = None,
    lang: str = "zh",
) -> Dict[str, Any]:
    """把一周的推荐推进 Zotero。

    永远返回字典，**不抛异常**：写不进去是常事（没配 key、Zotero 没开、
    网断了），不能因此让整周的报告变空白。

    ``collection_key`` 只有 Web API 那条路认；连接器这条路由 Zotero 的当前选中
    分类决定，传了也没用。

    返回 ``{"ok": bool, "count": int, "message": str}``。
    """
    if not papers:
        return {"ok": True, "count": 0, "message": pick(lang, "这一周没有需要推送的条目。", "Nothing to push.")}
    problem = config_error(lang)
    if problem:
        return {"ok": False, "count": 0, "message": problem}
    if use_connector():
        return push_via_connector(papers, week, lang=lang)
    items = [
        to_zotero_item(paper, week, rank=index, collection_key=collection_key)
        for index, paper in enumerate(papers, start=1)
    ]
    try:
        response = zotero_create(items, os.getenv("ZOTERO_USER_ID") or "", os.getenv("ZOTERO_API_KEY"))
    except Exception as exc:  # 网络/鉴权失败都不该让这一周白跑
        return {
            "ok": False,
            "count": 0,
            "message": pick(
                lang,
                f"Zotero 推送失败（{type(exc).__name__}: {exc}）。"
                f"阅读清单和账本都已写入，但 Zotero 里这次没有条目——建议手动导入这几篇，"
                f"否则它们不会带「{week_label(week)}」标签。",
                f"Zotero push failed ({type(exc).__name__}: {exc}). The reading list and ledger "
                f"were still written, but Zotero has nothing this week; import the papers by hand "
                f"so they keep the {week_label(week)} tag.",
            ),
        }
    failed = response.get("failed") or {}
    ok = response.get("successful") if isinstance(response, dict) else None
    count = len(ok) if isinstance(ok, dict) else len(items)
    message = pick(
        lang,
        f"已向 Zotero 推送 {count} 条，标签 {week_label(week)}。",
        f"Pushed {count} items to Zotero tagged {week_label(week)}.",
    )
    if failed:
        message += pick(lang, f"（有 {len(failed)} 条被 Zotero 拒绝）", f" ({len(failed)} rejected)")
    return {"ok": not failed, "count": count, "message": message}
