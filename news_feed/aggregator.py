"""
News Feed Aggregator — data source for hypothesis H1 (price lag after news).

Collects timestamped news items from multiple sources and matches them
to prediction markets by keyword / topic similarity.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from loguru import logger


@dataclass
class NewsItem:
    source: str  # rss / twitter / polymarket_description
    title: str
    url: str
    published_at: datetime
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    keywords: list[str] = field(default_factory=list)
    relevance_score: float = 0.0  # 0-1, how relevant to a specific market


@dataclass
class MarketNewsMatch:
    market_id: str
    market_question: str
    news: NewsItem
    lag_seconds: float  # time since news was published
    implied_direction: str  # "YES_UP" or "NO_UP" or "NEUTRAL"


class NewsFeedAggregator:
    """
    Aggregates news from RSS feeds, extracts keywords,
    and matches to prediction markets for H1 signal generation.
    """

    DEFAULT_RSS_FEEDS = [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.bbci.co.uk/news/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    ]

    def __init__(
        self,
        rss_feeds: list[str] | None = None,
        poll_interval_sec: int = 120,
        max_age_hours: int = 2,
    ):
        self._feeds = rss_feeds or self.DEFAULT_RSS_FEEDS
        self._poll_interval = poll_interval_sec
        self._max_age_hours = max_age_hours
        self._http = httpx.AsyncClient(timeout=15.0)
        self._seen_urls: set[str] = set()
        self._recent_news: list[NewsItem] = []

    async def close(self):
        await self._http.aclose()

    async def fetch_rss(self, feed_url: str) -> list[NewsItem]:
        """Fetch and parse a single RSS feed. Returns new items only."""
        items = []
        try:
            resp = await self._http.get(feed_url)
            resp.raise_for_status()
            xml = resp.text

            titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", xml)
            links = re.findall(r"<link>(.*?)</link>", xml)
            pub_dates = re.findall(r"<pubDate>(.*?)</pubDate>", xml)

            for i, (title_cdata, title_plain) in enumerate(titles):
                title = title_cdata or title_plain
                if not title:
                    continue

                url = links[i] if i < len(links) else ""
                if url in self._seen_urls:
                    continue
                self._seen_urls.add(url)

                pub_str = pub_dates[i] if i < len(pub_dates) else ""
                try:
                    from email.utils import parsedate_to_datetime
                    published = parsedate_to_datetime(pub_str)
                except Exception:
                    published = datetime.now(timezone.utc)

                keywords = self._extract_keywords(title)

                items.append(
                    NewsItem(
                        source="rss",
                        title=title,
                        url=url,
                        published_at=published,
                        keywords=keywords,
                    )
                )

        except Exception as e:
            logger.warning(f"RSS fetch failed for {feed_url}: {e}")

        return items

    async def fetch_all(self) -> list[NewsItem]:
        """Fetch all configured feeds and return new items."""
        tasks = [self.fetch_rss(url) for url in self._feeds]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        new_items = []
        for result in results:
            if isinstance(result, list):
                new_items.extend(result)

        self._recent_news = [
            n for n in (self._recent_news + new_items)
            if (datetime.now(timezone.utc) - n.published_at).total_seconds()
            < self._max_age_hours * 3600
        ]

        return new_items

    def match_to_markets(
        self,
        news_items: list[NewsItem],
        market_questions: dict[str, str],
        min_relevance: float = 0.3,
    ) -> list[MarketNewsMatch]:
        """
        Match news items to markets by keyword overlap.

        Args:
            news_items: Recent news.
            market_questions: {market_id: question_text}.
            min_relevance: Minimum keyword overlap score.
        """
        matches = []

        for market_id, question in market_questions.items():
            q_keywords = set(self._extract_keywords(question))
            if not q_keywords:
                continue

            for news in news_items:
                n_keywords = set(news.keywords)
                if not n_keywords:
                    continue

                overlap = len(q_keywords & n_keywords)
                score = overlap / max(len(q_keywords), 1)

                if score >= min_relevance:
                    lag = (datetime.now(timezone.utc) - news.published_at).total_seconds()
                    matches.append(
                        MarketNewsMatch(
                            market_id=market_id,
                            market_question=question,
                            news=news,
                            lag_seconds=lag,
                            implied_direction="NEUTRAL",
                        )
                    )

        matches.sort(key=lambda m: m.news.published_at, reverse=True)
        return matches

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """Extract meaningful keywords from text (simple tokenization)."""
        STOP_WORDS = {
            "the", "a", "an", "is", "are", "was", "were", "will", "be",
            "to", "of", "in", "for", "on", "at", "by", "with", "from",
            "and", "or", "but", "not", "this", "that", "it", "as", "if",
            "has", "have", "had", "do", "does", "did", "can", "could",
            "would", "should", "may", "might", "shall", "into", "than",
            "yes", "no", "what", "when", "where", "who", "how", "which",
        }
        words = re.findall(r"[a-zA-Z]{3,}", text.lower())
        return [w for w in words if w not in STOP_WORDS]

    @property
    def recent_news(self) -> list[NewsItem]:
        return list(self._recent_news)
