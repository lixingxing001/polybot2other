from __future__ import annotations

import email.utils
import html
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Callable

from .config import Settings


EVIDENCE_PROVIDER_BING_NEWS_RSS = "bing_news_rss"
EVIDENCE_SEARCH_URL = "https://www.bing.com/news/search"
EVIDENCE_USER_AGENT = "Mozilla/5.0 (compatible; polybot2other-evidence-scout/1.0)"
EVIDENCE_TEXT_MAX_CHARS = 360
EVIDENCE_QUERY_MAX_CHARS = 220

# 严格排除中文站点和中文内容，避免非 BTC 市场证据层混入用户明确禁止的信源。
BLOCKED_CHINESE_DOMAIN_PARTS = (
    ".cn",
    ".com.cn",
    ".net.cn",
    ".org.cn",
    ".com.hk",
    ".com.tw",
    "baidu.",
    "sina.",
    "sohu.",
    "qq.com",
    "163.com",
    "ifeng.",
    "people.cn",
    "chinadaily.com.cn",
    "globaltimes.cn",
    "scmp.com",
    "caixin.",
    "guancha.",
    "thepaper.cn",
    "xinhua.",
)
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


UrlopenFunc = Callable[..., Any]


class MarketEvidenceScout:
    """非 BTC 市场外部证据搜索器；只返回结构化证据，不做下注决策。"""

    def __init__(self, settings: Settings, *, urlopen: UrlopenFunc | None = None) -> None:
        self.settings = settings
        self.urlopen = urlopen or urllib.request.urlopen
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def enrich_candidates(
        self,
        candidates: list[dict[str, Any]],
        runtime_settings: dict[str, Any],
        *,
        now: float | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """给候选市场补充外部新闻证据；网络失败只降级为 evidence.error。"""

        at = time.time() if now is None else float(now)
        enabled = bool(runtime_settings.get("evidence_enabled", self.settings.market_scout_evidence_enabled))
        max_markets = _bounded_int(
            runtime_settings.get("evidence_max_markets", self.settings.market_scout_evidence_max_markets),
            self.settings.market_scout_evidence_max_markets,
            0,
            20,
        )
        results_per_market = _bounded_int(
            runtime_settings.get(
                "evidence_results_per_market",
                self.settings.market_scout_evidence_results_per_market,
            ),
            self.settings.market_scout_evidence_results_per_market,
            1,
            8,
        )
        ttl_seconds = _bounded_float(
            runtime_settings.get("evidence_ttl_seconds", self.settings.market_scout_evidence_ttl_seconds),
            self.settings.market_scout_evidence_ttl_seconds,
            30.0,
            86_400.0,
        )
        timeout_seconds = _bounded_float(
            runtime_settings.get("evidence_timeout_seconds", self.settings.market_scout_evidence_timeout_seconds),
            self.settings.market_scout_evidence_timeout_seconds,
            1.0,
            30.0,
        )
        if not enabled:
            return [dict(candidate) for candidate in candidates], {
                "enabled": False,
                "provider": EVIDENCE_PROVIDER_BING_NEWS_RSS,
                "searched_count": 0,
                "ok_count": 0,
                "error_count": 0,
                "no_result_count": 0,
                "skipped_count": len(candidates),
                "details": ["Evidence Scout 已关闭"],
            }

        enriched: list[dict[str, Any]] = []
        report = {
            "enabled": True,
            "provider": EVIDENCE_PROVIDER_BING_NEWS_RSS,
            "searched_count": 0,
            "ok_count": 0,
            "error_count": 0,
            "no_result_count": 0,
            "skipped_count": 0,
            "details": [],
        }
        for index, candidate in enumerate(candidates):
            item = dict(candidate)
            if index >= max_markets:
                evidence = {
                    "status": "skipped",
                    "provider": EVIDENCE_PROVIDER_BING_NEWS_RSS,
                    "query": "",
                    "searched_at": at,
                    "results": [],
                    "result_count": 0,
                    "notes": ["超过本轮证据搜索市场数量上限"],
                }
                report["skipped_count"] = int(report["skipped_count"]) + 1
            else:
                evidence = self.search_candidate(
                    item,
                    now=at,
                    results_per_market=results_per_market,
                    ttl_seconds=ttl_seconds,
                    timeout_seconds=timeout_seconds,
                )
                report["searched_count"] = int(report["searched_count"]) + 1
                status = str(evidence.get("status") or "")
                if status == "ok":
                    report["ok_count"] = int(report["ok_count"]) + 1
                elif status == "error":
                    report["error_count"] = int(report["error_count"]) + 1
                elif status == "no_results":
                    report["no_result_count"] = int(report["no_result_count"]) + 1
            item["evidence"] = evidence
            item["evidence_status"] = evidence.get("status")
            item["evidence_result_count"] = evidence.get("result_count", 0)
            details = report["details"]
            if isinstance(details, list) and len(details) < 8:
                details.append(_evidence_report_line(item, evidence))
            enriched.append(item)
        return enriched, report

    def search_candidate(
        self,
        candidate: dict[str, Any],
        *,
        now: float,
        results_per_market: int,
        ttl_seconds: float,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        queries = build_evidence_queries(candidate)
        if not queries:
            return {
                "status": "no_query",
                "provider": EVIDENCE_PROVIDER_BING_NEWS_RSS,
                "query": "",
                "searched_at": now,
                "results": [],
                "result_count": 0,
                "notes": ["候选市场缺少可搜索文本"],
            }
        last_payload: dict[str, Any] | None = None
        for query in queries:
            cache_key = f"{EVIDENCE_PROVIDER_BING_NEWS_RSS}:{query.lower()}:{results_per_market}"
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] <= ttl_seconds:
                payload = dict(cached[1])
                payload["cached"] = True
            else:
                payload = self._search_query(query, results_per_market, timeout_seconds, now)
                self._cache[cache_key] = (now, payload)
                self._prune_cache(now, ttl_seconds)
            last_payload = payload
            if payload.get("status") == "ok":
                return dict(payload)
        return dict(last_payload or {})

    def _search_query(
        self,
        query: str,
        results_per_market: int,
        timeout_seconds: float,
        now: float,
    ) -> dict[str, Any]:
        try:
            results, blocked_count = self._fetch_bing_news(query, results_per_market, timeout_seconds, now)
            status = "ok" if results else "no_results"
            return {
                "status": status,
                "provider": EVIDENCE_PROVIDER_BING_NEWS_RSS,
                "query": query,
                "searched_at": now,
                "results": results,
                "result_count": len(results),
                "blocked_count": blocked_count,
                "cached": False,
                "notes": _evidence_notes(results, blocked_count),
            }
        except Exception as exc:  # noqa: BLE001 - 证据搜索失败只能影响本轮证据，不能影响扫描线程。
            return {
                "status": "error",
                "provider": EVIDENCE_PROVIDER_BING_NEWS_RSS,
                "query": query,
                "searched_at": now,
                "results": [],
                "result_count": 0,
                "blocked_count": 0,
                "cached": False,
                "error": f"{type(exc).__name__}: {exc}",
                "notes": ["证据搜索失败，本候选只能依赖 Polymarket 元数据和盘口"],
            }

    def _fetch_bing_news(
        self,
        query: str,
        limit: int,
        timeout_seconds: float,
        now: float,
    ) -> tuple[list[dict[str, Any]], int]:
        params = urllib.parse.urlencode(
            {
                "q": query,
                "format": "RSS",
                "mkt": "en-US",
                "setlang": "en-US",
            }
        )
        request = urllib.request.Request(
            f"{EVIDENCE_SEARCH_URL}?{params}",
            headers={"User-Agent": EVIDENCE_USER_AGENT},
        )
        with self.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(240_000)
        root = ET.fromstring(raw)
        results: list[dict[str, Any]] = []
        blocked_count = 0
        for item in root.findall(".//item"):
            parsed = _parse_rss_item(item, now)
            if not parsed:
                continue
            if _is_blocked_chinese_source(parsed):
                blocked_count += 1
                continue
            results.append(parsed)
            if len(results) >= limit:
                break
        return results, blocked_count

    def _prune_cache(self, now: float, ttl_seconds: float) -> None:
        if len(self._cache) <= 200:
            return
        expired = [key for key, (created_at, _payload) in self._cache.items() if now - created_at > ttl_seconds]
        for key in expired:
            self._cache.pop(key, None)
        while len(self._cache) > 160:
            oldest_key = min(self._cache.items(), key=lambda item: item[1][0])[0]
            self._cache.pop(oldest_key, None)


def build_evidence_query(candidate: dict[str, Any]) -> str:
    """生成英文新闻检索词；尽量保留市场核心问题，避免宽泛查询引入噪声。"""

    queries = build_evidence_queries(candidate)
    return queries[0] if queries else ""


def build_evidence_queries(candidate: dict[str, Any]) -> list[str]:
    """生成多级英文检索词；完整问题无结果时回退到关键词查询。"""

    parts: list[str] = []
    for key in ("question", "event_title"):
        text = _clean_text(candidate.get(key))
        if text and not _contains_cjk(text) and text.lower() not in {part.lower() for part in parts}:
            parts.append(text)
    query = " ".join(parts)
    query = SPACE_RE.sub(" ", query)
    if not query:
        return []
    keywords = _keyword_query(query)
    short_keywords = " ".join(keywords.split()[:4]) or keywords
    candidates = [
        f"{query} latest news",
        f"{short_keywords} latest news",
        f"{short_keywords} Reuters AP",
        f"{short_keywords} official",
    ]
    cleaned: list[str] = []
    for item in candidates:
        normalized = SPACE_RE.sub(" ", item).strip(" ?")
        if normalized and normalized.lower() not in {existing.lower() for existing in cleaned}:
            cleaned.append(normalized[:EVIDENCE_QUERY_MAX_CHARS].strip())
    return cleaned


def _keyword_query(text: str) -> str:
    stopwords = {
        "will",
        "the",
        "and",
        "or",
        "yes",
        "no",
        "by",
        "before",
        "after",
        "during",
        "with",
        "from",
        "into",
        "this",
        "that",
        "market",
        "prediction",
        "predictions",
    }
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{1,}", text)
    selected: list[str] = []
    for token in tokens:
        normalized = token.lower()
        if normalized in stopwords:
            continue
        if normalized not in {item.lower() for item in selected}:
            selected.append(token)
        if len(selected) >= 10:
            break
    return " ".join(selected) or SPACE_RE.sub(" ", text).strip()


def _parse_rss_item(item: ET.Element, now: float) -> dict[str, Any] | None:
    title = _clean_text(item.findtext("title"))
    raw_link = _clean_text(item.findtext("link"))
    snippet = _clean_text(item.findtext("description"))
    published_raw = _clean_text(item.findtext("pubDate"))
    source = ""
    for child in item:
        tag = child.tag.split("}", 1)[-1].lower()
        if tag == "source":
            source = _clean_text(child.text)
            break
    url = _extract_bing_final_url(raw_link)
    domain = _domain_from_url(url)
    if not title or not url or not domain:
        return None
    published_at = _parse_pubdate(published_raw)
    age_hours = round((now - published_at) / 3600.0, 3) if published_at else None
    return {
        "title": title[:EVIDENCE_TEXT_MAX_CHARS],
        "url": url,
        "domain": domain,
        "source": source[:120],
        "snippet": snippet[:EVIDENCE_TEXT_MAX_CHARS],
        "published_at": published_at,
        "published_raw": published_raw[:80],
        "age_hours": age_hours,
    }


def _extract_bing_final_url(raw_link: str) -> str:
    if not raw_link:
        return ""
    link = html.unescape(raw_link)
    parsed = urllib.parse.urlparse(link)
    query = urllib.parse.parse_qs(parsed.query)
    final = query.get("url", [""])[0]
    return final or link


def _domain_from_url(url: str) -> str:
    try:
        domain = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return ""
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def _is_blocked_chinese_source(result: dict[str, Any]) -> bool:
    domain = str(result.get("domain") or "").lower()
    for blocked in BLOCKED_CHINESE_DOMAIN_PARTS:
        if blocked in domain:
            return True
    text = " ".join(str(result.get(key) or "") for key in ("title", "snippet", "source", "domain"))
    return _contains_cjk(text)


def _contains_cjk(text: str) -> bool:
    return bool(CJK_RE.search(str(text or "")))


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = TAG_RE.sub(" ", text)
    text = SPACE_RE.sub(" ", text).strip()
    return text


def _parse_pubdate(value: str) -> float | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed.timestamp()


def _evidence_notes(results: list[dict[str, Any]], blocked_count: int) -> list[str]:
    notes: list[str] = []
    if not results:
        notes.append("未找到可用英文新闻证据")
    for result in results[:3]:
        label = result.get("source") or result.get("domain") or "source"
        notes.append(f"{label}: {result.get('title')}")
    if blocked_count:
        notes.append(f"已过滤中文或禁止信源 {blocked_count} 条")
    return notes[:5]


def _evidence_report_line(candidate: dict[str, Any], evidence: dict[str, Any]) -> str:
    question = str(candidate.get("question") or candidate.get("slug") or "-")[:80]
    status = str(evidence.get("status") or "-")
    count = int(evidence.get("result_count") or 0)
    query = str(evidence.get("query") or "")[:90]
    return f"{question} | {status} | results={count} | {query}"


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(minimum, min(maximum, parsed))
