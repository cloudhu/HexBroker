"""情报服务（§3.1 IntelligenceService / D5 / R7-R8）。

Provider 接口隔离（MCP 桥接 P1，§8.3）：MVP 用静态 provider（离线 JSON），
公开新闻源可后续按接口接入（mx-ds-mcp / westock 桥接无侵入）。

情报影响面（Q3 批复）：仅「风险提示 + 计划备注」，不自动改方向/仓位。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol

from ..utils.logging import get_logger
from .types import NewsItem

log = get_logger("PAPER")


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
        static_file = paper_cfg.get("intel_static_file")
        providers: list[IntelligenceProvider] = []
        if static_file:
            providers.append(StaticNewsProvider(path=str(static_file)))
        return cls(
            providers=providers,
            keywords=dict(intel_cfg.get("keywords", {}) or {}),
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