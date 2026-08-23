"""情报服务（§3.1 IntelligenceService / D5 / R7-R8）。

Provider 接口隔离（MCP 桥接 P1，§8.3）：
- ``StaticNewsProvider``：读离线 JSON（MCP 文件桥接——agent 用 mx-ds-mcp 拉取落盘后由本进程读取）。
- ``HTTPNewsProvider``：东财公开检索 API（MCP 替代——独立进程可直接 HTTP 调用，自包含零依赖）。

情报影响面（Q3 批复）：仅「风险提示 + 计划备注」，不自动改方向/仓位。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol

from ..utils.logging import get_logger
from .types import NewsItem

log = get_logger("PAPER")

_EM_TAG = re.compile(r"</?em>")


class IntelligenceProvider(Protocol):
    """情报源接口（§3.1）。"""

    def fetch_news(self, symbols: list[str], since: datetime) -> list[NewsItem]:
        """拉取 ``since`` 之后、与 ``symbols`` 相关的情报。"""
        ...

    def name(self) -> str:
        """源名称。"""
        ...


class StaticNewsProvider:
    """静态情报源：读取可配置 JSON 文件（无文件/无条目 → 空）。

    文件格式（§3.4）::

        [
          {"ts": "2026-08-23T10:30:00", "symbols": ["ag0"],
           "title": "...", "summary": "...", "tags": ["risk"], "source": "static"}
        ]
    """

    def __init__(self, path: Optional[str | Path] = None, name: str = "static") -> None:
        self._path = Path(path) if path else None
        self._name = name

    def name(self) -> str:
        return self._name

    def fetch_news(self, symbols: list[str], since: datetime) -> list[NewsItem]:
        if self._path is None or not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("静态情报解析失败 path={} err={}", self._path, exc)
            return []
        out: list[NewsItem] = []
        for item in raw or []:
            try:
                ts = datetime.fromisoformat(item.get("ts", ""))
                if ts < since:
                    continue
                item_symbols = [s for s in (item.get("symbols") or []) if s in symbols]
                if not item_symbols:
                    continue
                out.append(
                    NewsItem(
                        ts=ts,
                        symbols=item_symbols,
                        title=str(item.get("title", "")),
                        summary=str(item.get("summary", "")),
                        tags=list(item.get("tags", []) or []),
                        source=str(item.get("source", self._name)),
                    )
                )
            except (ValueError, TypeError):
                continue
        return out


class HTTPNewsProvider:
    """东财公开检索 API 情报源（MCP 桥接：独立进程可直接 HTTP 调用）。

    说明：mx-ds-mcp / westock 为平台托管 MCP（仅 agent 可调），独立 Python 进程
    无法 import——本 Provider 用东财公开搜索接口（search-api-web.eastmoney.com）
    实现真实市场情报，效果等同且自包含；另保留 MCP 文件桥接路径（agent 拉取
    落盘 JSON → StaticNewsProvider 读取）。
    """

    _SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"

    def __init__(
        self,
        keyword_map: Optional[dict[str, list[str]]] = None,
        name: str = "eastmoney",
        timeout: float = 10.0,
    ) -> None:
        # 品种 → 搜索/过滤关键词（与 IntelligenceService.keywords 同源）
        self._keyword_map: dict[str, list[str]] = keyword_map or {}
        self._name = name
        self._timeout = float(timeout)

    def name(self) -> str:
        return self._name

    # ------------------------------------------------------------------
    # 内部：单关键词搜索
    # ------------------------------------------------------------------
    def _search(self, keyword: str, since: datetime, page_size: int = 5) -> list[NewsItem]:
        param = {
            "uid": "",
            "keyword": keyword,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": page_size,
                    "preTag": "<em>",
                    "postTag": "</em>",
                }
            },
        }
        url = f"{self._SEARCH_URL}?cb=cb&param=" + urllib.parse.quote(json.dumps(param))
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://so.eastmoney.com/"},
        )
        raw = urllib.request.urlopen(req, timeout=self._timeout).read().decode("utf-8", "ignore")
        m = re.match(r"^cb\((.*)\)$", raw, re.S)
        if not m:
            return []
        data = json.loads(m.group(1))
        arts = (data.get("result") or {}).get("cmsArticleWebOld") or []
        out: list[NewsItem] = []
        for a in arts:
            try:
                ts = datetime.strptime(str(a.get("date", "")), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if ts < since:
                continue
            title = _EM_TAG.sub("", str(a.get("title", ""))).strip()
            summary = _EM_TAG.sub("", str(a.get("content", ""))).strip()
            if not title:
                continue
            out.append(
                NewsItem(
                    ts=ts,
                    symbols=[],  # 由 IntelligenceService.filter_by_keywords 归类
                    title=title,
                    summary=summary,
                    tags=[],
                    source=str(a.get("mediaName") or self._name),
                )
            )
        return out

    # ------------------------------------------------------------------
    # Provider 接口
    # ------------------------------------------------------------------
    def fetch_news(self, symbols: list[str], since: datetime) -> list[NewsItem]:
        """按品种关键词搜索并去重（同 url 不重复——此处按 title 近似去重）。"""
        out: list[NewsItem] = []
        seen: set[str] = set()
        for sym in symbols:
            kws = self._keyword_map.get(sym) or [sym]
            for kw in kws:
                try:
                    items = self._search(kw, since)
                except Exception as exc:
                    log.error("情报源 {} 关键词 '{}' 拉取失败 err={}", self._name, kw, exc)
                    continue
                for item in items:
                    key = item.title
                    if key in seen:
                        continue
                    seen.add(key)
                    item.symbols = [sym]
                    out.append(item)
        return out


class IntelligenceService:
    """情报聚合服务：多源拉取 → 关键词过滤/归类 → 结构化 NewsItem。"""

    def __init__(
        self,
        providers: Optional[list[IntelligenceProvider]] = None,
        keywords: Optional[dict[str, list[str]]] = None,
        risk_keywords: Optional[list[str]] = None,
    ) -> None:
        self.providers: list[IntelligenceProvider] = list(providers or [])
        self.keywords: dict[str, list[str]] = keywords or {}
        self.risk_keywords: list[str] = risk_keywords or []

    @classmethod
    def from_config(cls, paper_cfg: Any) -> "IntelligenceService":
        intel_cfg = paper_cfg.get("intel", {}) or {}
        keywords: dict[str, list[str]] = dict(intel_cfg.get("keywords", {}) or {})
        providers: list[IntelligenceProvider] = []
        # 静态源（MCP 文件桥接：agent 用 mx-ds-mcp 拉取落盘 JSON）
        static_file = paper_cfg.get("intel_static_file")
        if static_file:
            providers.append(StaticNewsProvider(path=str(static_file)))
        # HTTP 真实情报源（东财公开 API，MCP 替代）
        http_cfg = intel_cfg.get("http", {}) or {}
        if http_cfg.get("enabled", False):
            providers.append(
                HTTPNewsProvider(
                    keyword_map=keywords,
                    timeout=float(http_cfg.get("timeout_sec", 10)),
                )
            )
        return cls(
            providers=providers,
            keywords=keywords,
            risk_keywords=list(intel_cfg.get("risk_keywords", []) or []),
        )

    def poll(self, symbols: list[str], since: datetime) -> list[NewsItem]:
        """轮询全部 provider 并做关键词过滤/归类。"""
        items: list[NewsItem] = []
        for provider in self.providers:
            try:
                items.extend(provider.fetch_news(symbols, since))
            except Exception as exc:
                log.error("情报源 {} 拉取失败 err={}", provider.name(), exc)
        return self.filter_by_keywords(items)

    def filter_by_keywords(self, items: list[NewsItem]) -> list[NewsItem]:
        """按品种关键词过滤并归类；命中风险词 → 打 risk 标签。"""
        out: list[NewsItem] = []
        for item in items or []:
            text = f"{item.title} {item.summary}"
            matched = [s for s, kws in self.keywords.items() if any(k in text for k in kws)]
            if not matched:
                continue
            item.symbols = matched
            tags = set(item.tags)
            if any(rk in text for rk in self.risk_keywords):
                tags.add("risk")
            item.tags = sorted(tags)
            out.append(item)
        return out