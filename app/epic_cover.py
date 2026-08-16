"""Epic 封面获取：经 Epic Games Store 官方 GraphQL 按游戏名搜索封面。

- 端点：https://store.epicgames.com/graphql（匿名 searchStore）
- 请求与图片下载全部走 security.safe_get：HTTPS + host 白名单(epicgames.com)
  + 解析后 IP 非私网/环回（防 SSRF / DNS rebinding）
- 搜索返回多个结果（如 "Hades" 会带出 Hades II / Soundtrack 等），
  用归一化名称相似度过滤，避免张冠李戴。
- 支持一次传多个候选名（如中文名 + Epic 清单 DisplayName 英文名），
  提高"库里存的是中文名"时也能搜到官方封面。
"""
import difflib
import json

from .audit import get_logger
from .security import EPIC_IMAGE_HOSTS, safe_get

_log = get_logger()

_GRAPHQL_URL = "https://store.epicgames.com/graphql"
_QUERY = ("query searchStore($k: String, $c: Int) { "
          "Catalog { searchStore(keywords: $k, count: $c) { "
          "elements { title keyImages { type url } } } } }")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# 相似度阈值：Epic 结果常带 "+ Soundtrack" 等后缀；
# 0.6 比旧值 0.75 更宽容，中文名/简称也能命中，DLC/原声带靠相似度排序压后
_MATCH_THRESHOLD = 0.6


def _norm(s: str) -> str:
    """名称归一化：小写 + 去空白与常见噪音词（和 potatoVN 思路一致）。"""
    return "".join((s or "").lower().split())


def _score_pair(query_norm: str, title_norm: str) -> float:
    """两个归一化名称的匹配分：
    - 完全相同 → 1.0；
    - 互相包含（"Godofwar" 在 "Godofwarragnarok" 里）→ 高分 0.95；
    - 否则用 SequenceMatcher 相似度。"""
    if not query_norm or not title_norm:
        return 0.0
    if query_norm == title_norm:
        return 1.0
    if query_norm in title_norm or title_norm in query_norm:
        return 0.95
    return difflib.SequenceMatcher(None, query_norm, title_norm).ratio()


def search_epic_covers(queries, timeout=15, cancel=None) -> list:
    """按（一个或多个）候选名搜索 Epic 封面。

    queries: str 或 [str, ...]。多个名字会依次请求，结果合并去重，
    按最高相似度排序返回 [{title, cover_url, score}]（仅取 OfferImageTall 竖版盒图）。
    失败/无结果返回空列表。
    """
    if isinstance(queries, str):
        queries = [queries]
    out = []
    seen_covers = set()
    for query in queries:
        query = (query or "").strip()
        if not query:
            continue
        if cancel is not None and cancel.is_set():
            return []
        payload = {"query": _QUERY, "variables": {"k": query, "c": 10}}
        body = safe_get(
            _GRAPHQL_URL, EPIC_IMAGE_HOSTS, timeout=timeout, max_hops=2,
            method="POST", json_body=payload,
            label="Epic封面搜索",
            headers={"Content-Type": "application/json", "User-Agent": _UA,
                     "Origin": "https://store.epicgames.com",
                     "Referer": "https://store.epicgames.com/"})
        if cancel is not None and cancel.is_set():
            return []
        if not body:
            _log.warning("Epic 封面搜索无响应 query=%s", query[:40])
            continue
        try:
            data = json.loads(body)
            elements = (data.get("data", {}).get("Catalog", {})
                        .get("searchStore", {}).get("elements", []))
        except (ValueError, AttributeError):
            _log.warning("Epic 封面搜索响应解析失败 query=%s", query[:40])
            continue

        qn = _norm(query)
        for e in elements:
            title = e.get("title") or ""
            cover = next((k.get("url") for k in e.get("keyImages", [])
                          if k.get("type") == "OfferImageTall"), "")
            if not cover or cover in seen_covers:
                continue
            score = _score_pair(qn, _norm(title))
            if score < _MATCH_THRESHOLD:
                continue
            seen_covers.add(cover)
            out.append({"title": title, "cover_url": cover, "score": score})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out