"""yt-dlp wrapper (plan2.md §7).

Search uses ``ytsearch<N>:<query>`` with ``--flat-playlist -J`` so it returns
ids and titles without resolving stream URLs - that is fast.  Full URL
resolution costs ~2-5 s per video (nsig decoding), so it happens lazily in a
background thread while item 1 is already playing, which is what makes
``proximo`` feel instant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import yt_dlp

LOGGER = logging.getLogger(__name__)

WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
# Audio-only, best bitrate first; yt-dlp picks the container.
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"


class SearchError(RuntimeError):
    """Raised when yt-dlp cannot search or resolve."""


@dataclass(frozen=True)
class SearchResult:
    """One search hit.  ``url`` is a watch URL, not a stream URL."""

    video_id: str
    title: str
    url: str
    duration: float | None = None


@dataclass(frozen=True)
class ResolvedStream:
    """A directly playable stream URL plus the headers it needs."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)


class Searcher:
    """Thin yt-dlp facade used by the command handlers."""

    def __init__(
        self,
        results: int = 5,
        cookies_from_browser: str | None = None,
        socket_timeout_s: float = 15.0,
    ) -> None:
        self.results = int(results)
        self.cookies_from_browser = cookies_from_browser
        self.socket_timeout_s = float(socket_timeout_s)

    # -- options --------------------------------------------------------- #

    def _options(self, **extra: Any) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "skip_download": True,
            "socket_timeout": self.socket_timeout_s,
            "retries": 2,
            "extractor_retries": 2,
            "logger": _QuietLogger(),
        }
        if self.cookies_from_browser:
            options["cookiesfrombrowser"] = (self.cookies_from_browser,)
        options.update(extra)
        return options

    @staticmethod
    def _hint() -> str:
        return (
            "YouTube may be applying a bot check; set search.cookies_from_browser "
            "(e.g. firefox) in config.yaml, or update yt-dlp"
        )

    # -- API ------------------------------------------------------------- #

    def search(self, query: str) -> list[SearchResult]:
        """Return up to ``self.results`` hits for *query*, fastest possible."""
        query = (query or "").strip()
        if not query:
            return []
        search_term = f"ytsearch{self.results}:{query}"
        LOGGER.debug("yt-dlp search: %s", search_term)
        try:
            with yt_dlp.YoutubeDL(self._options(extract_flat="in_playlist")) as ydl:
                info = ydl.extract_info(search_term, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise SearchError(f"search failed for {query!r}: {exc}. {self._hint()}") from exc

        results: list[SearchResult] = []
        for entry in (info or {}).get("entries") or []:
            if not entry:
                continue
            video_id = str(entry.get("id") or "")
            url = str(entry.get("url") or "")
            if not video_id and url:
                video_id = url.rsplit("=", 1)[-1]
            if not url and video_id:
                url = WATCH_URL.format(video_id=video_id)
            if not video_id or not url:
                continue
            results.append(
                SearchResult(
                    video_id=video_id,
                    title=str(entry.get("title") or video_id),
                    url=url,
                    duration=entry.get("duration"),
                )
            )
        LOGGER.info("search %r -> %d result(s)", query, len(results))
        return results

    def resolve(self, url_or_result: str | SearchResult) -> ResolvedStream:
        """Resolve a watch URL into a directly playable stream URL."""
        url = url_or_result.url if isinstance(url_or_result, SearchResult) else str(url_or_result)
        try:
            with yt_dlp.YoutubeDL(self._options(format=AUDIO_FORMAT)) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise SearchError(f"resolve failed for {url}: {exc}. {self._hint()}") from exc

        if info and info.get("entries"):
            info = info["entries"][0]
        stream_url = (info or {}).get("url")
        if not stream_url:
            # Some extractors only give the URL inside the selected format.
            formats = (info or {}).get("formats") or []
            audio_only = [
                fmt for fmt in formats if fmt.get("acodec") not in (None, "none") and fmt.get("url")
            ]
            if audio_only:
                stream_url = audio_only[-1]["url"]
        if not stream_url:
            raise SearchError(f"yt-dlp returned no playable stream for {url}")

        headers = {
            str(key): str(value)
            for key, value in ((info or {}).get("http_headers") or {}).items()
        }
        return ResolvedStream(url=str(stream_url), headers=headers)


class _QuietLogger:
    """Route yt-dlp messages into logging instead of stderr noise."""

    def debug(self, message: str) -> None:
        if message.startswith("[debug] "):
            LOGGER.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        LOGGER.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        LOGGER.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        LOGGER.error("yt-dlp: %s", message)