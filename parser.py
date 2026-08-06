"""
网易云音乐 URL 解析模块。

负责：
1. 从文本中提取 music.163.com 歌曲/专辑/歌单/播客链接（含 QQ 卡片和短链接）
2. 解析 163cn.tv 短链接为真实 URL
3. 数据结构定义

移植自 HIKARI BOT NEO 的 netease_parser 插件。代码独立，不依赖任何宿主机器人模块。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from astrbot.api import logger

# =========================
# URL 正则
# =========================

# 匹配 music.163.com 的歌曲链接
NETEASE_SONG_URL_RE = re.compile(
    r"(?:https?://)?"
    r"(?:(?:www|y)\.)?music\.163\.com"
    r"(?:/#)?"
    r"(?:/m)?/song"
    r"(?:/(?P<id_path>\d{5,12})(?:/\?[^\s]*)?(?:\?[^\s]*)?"
    r"|\?(?:[^\s]*?&)?id=(?P<id_query>\d{5,12}))",
    re.IGNORECASE,
)

# 匹配 163cn.tv 短链接（QQ 卡片分享常用）
NETEASE_SHORT_URL_RE = re.compile(
    r"(?:https?://)?163cn\.tv/[A-Za-z0-9]+",
    re.IGNORECASE,
)

# 匹配 music.163.com 的专辑链接
NETEASE_ALBUM_URL_RE = re.compile(
    r"(?:https?://)?"
    r"(?:(?:www|y)\.)?music\.163\.com"
    r"(?:/#)?"
    r"(?:/m)?/album"
    r"(?:/(?P<id_path>\d{5,12})(?:/\?[^\s]*)?(?:\?[^\s]*)?"
    r"|\?(?:[^\s]*?&)?id=(?P<id_query>\d{5,12}))",
    re.IGNORECASE,
)

# 匹配网易云音乐播客/电台节目链接
NETEASE_PROGRAM_URL_RE = re.compile(
    r"(?:https?://)?(?:y\.)?music\.163\.com"
    r"(?:/m)?/program\?(?:[^\s]*?&)?id=(?P<id>\d{5,12})",
    re.IGNORECASE,
)

# 匹配 music.163.com 的歌单链接
NETEASE_PLAYLIST_URL_RE = re.compile(
    r"(?:https?://)?"
    r"(?:(?:www|y)\.)?music\.163\.com"
    r"(?:/#)?"
    r"(?:/m)?/playlist"
    r"(?:/(?P<id_path>\d{5,12})(?:/\?[^\s]*)?(?:\?[^\s]*)?"
    r"|\?(?:[^\s]*?&)?id=(?P<id_query>\d{5,12}))",
    re.IGNORECASE,
)

GENERIC_URL_RE = re.compile(r"https?://[^\s\"'>]+", re.IGNORECASE)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.7103.48 Safari/537.36"
)


# =========================
# 数据结构
# =========================


@dataclass
class NeteaseSongInfo:
    """歌曲基本信息。"""

    id: str
    name: str = ""
    artist: str = ""
    album: str = ""
    pic_url: str = ""


@dataclass
class NeteaseSongUrlResult:
    """歌曲音频 URL 查询结果。"""

    url: str = ""
    br: int = 0
    size: int = 0
    type: str = "mp3"
    code: int = 200


# =========================
# URL 提取
# =========================


def _unescape_cq(text: str) -> str:
    """还原 OneBot V11 CQ 码转义，方便正则匹配 URL。"""
    return text.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def extract_all_urls(text: str, card_data: list[dict[str, Any]] | None = None) -> list[str]:
    """从消息正文和卡片元数据中提取所有可能的 URL。"""
    urls: list[str] = []
    seen: set[str] = set()

    for match in GENERIC_URL_RE.finditer(str(text or "")):
        url = match.group(0).rstrip(".,;:!?)")
        if url not in seen:
            seen.add(url)
            urls.append(url)

    for data in card_data or []:
        for url in _extract_urls_from_data(data):
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

    if urls:
        logger.debug(f"[Netease] extract_all_urls 共提取到 {len(urls)} 个 URL")
    return urls


def _extract_urls_from_data(data: Any) -> list[str]:
    """从单个卡片 data 中提取 URL（QQ 音乐分享卡片等）。"""
    urls: list[str] = []
    try:
        if isinstance(data, str):
            if data.startswith("{"):
                parsed = json.loads(data)
                url = _extract_qqdocurl(parsed)
                if url:
                    urls.append(url)
        elif isinstance(data, dict):
            url = _extract_qqdocurl(data)
            if url:
                urls.append(url)
            inner = data.get("data")
            if isinstance(inner, str) and inner.startswith("{"):
                urls.extend(_extract_urls_from_data(inner))
    except (AttributeError, KeyError, json.JSONDecodeError, TypeError):
        pass
    return urls


def _extract_qqdocurl(data: Any) -> Optional[str]:
    """从 QQ 卡片元数据中提取跳转 URL。"""
    if not isinstance(data, dict):
        return None
    meta = data.get("meta") or {}
    if isinstance(meta, dict):
        detail_1 = meta.get("detail_1") or {}
        if isinstance(detail_1, dict):
            url = detail_1.get("qqdocurl")
            if url and isinstance(url, str):
                return url
        news = meta.get("news") or {}
        if isinstance(news, dict):
            url = news.get("jumpUrl")
            if url and isinstance(url, str):
                return url
        music = meta.get("music") or {}
        if isinstance(music, dict):
            url = music.get("jumpUrl")
            if url and isinstance(url, str):
                return url
    return None


def extract_song_ids(text: str) -> list[str]:
    """从文本中提取所有歌曲 ID（去重，保持顺序）。"""
    ids: list[str] = []
    seen: set[str] = set()
    clean = _unescape_cq(text)
    for match in NETEASE_SONG_URL_RE.finditer(clean):
        song_id = match.group("id_path") or match.group("id_query")
        if song_id and song_id not in seen:
            seen.add(song_id)
            ids.append(song_id)
    return ids


def extract_program_ids(text: str) -> list[str]:
    """从文本中提取所有播客/电台节目 ID（去重，保持顺序）。"""
    ids: list[str] = []
    seen: set[str] = set()
    clean = _unescape_cq(text)
    for match in NETEASE_PROGRAM_URL_RE.finditer(clean):
        pid = match.group("id")
        if pid and pid not in seen:
            seen.add(pid)
            ids.append(pid)
    return ids


def extract_album_ids(text: str) -> list[str]:
    """从文本中提取所有专辑 ID（去重，保持顺序）。"""
    ids: list[str] = []
    seen: set[str] = set()
    clean = _unescape_cq(text)
    for match in NETEASE_ALBUM_URL_RE.finditer(clean):
        album_id = match.group("id_path") or match.group("id_query")
        if album_id and album_id not in seen:
            seen.add(album_id)
            ids.append(album_id)
    return ids


def extract_playlist_ids(text: str) -> list[str]:
    """从文本中提取所有歌单 ID（去重，保持顺序）。"""
    ids: list[str] = []
    seen: set[str] = set()
    clean = _unescape_cq(text)
    for match in NETEASE_PLAYLIST_URL_RE.finditer(clean):
        playlist_id = match.group("id_path") or match.group("id_query")
        if playlist_id and playlist_id not in seen:
            seen.add(playlist_id)
            ids.append(playlist_id)
    return ids


def extract_id_from_url(url: str, pattern: re.Pattern) -> Optional[str]:
    """从单个 URL 中提取 ID。"""
    match = pattern.search(url)
    if not match:
        return None
    return match.group("id_path") or match.group("id_query") or match.group("id")


def has_netease_url(text: str) -> bool:
    """检查文本中是否包含网易云音乐相关链接。"""
    clean = _unescape_cq(text)
    return bool(
        NETEASE_SONG_URL_RE.search(clean)
        or NETEASE_SHORT_URL_RE.search(text)
        or NETEASE_PROGRAM_URL_RE.search(clean)
        or NETEASE_ALBUM_URL_RE.search(clean)
        or NETEASE_PLAYLIST_URL_RE.search(clean)
    )


async def resolve_short_url(short_url: str, timeout: int = 10) -> Optional[str]:
    """解析 163cn.tv 短链接，跟随重定向获取真实 URL。失败返回 None。"""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0),
            follow_redirects=True,
            max_redirects=5,
        ) as client:
            resp = await client.get(short_url, headers={"User-Agent": USER_AGENT})
            final_url = str(resp.url)
            if final_url and final_url != short_url:
                logger.info(f"[Netease] 短链接解析成功 → {short_url} → {final_url}")
                return final_url
    except httpx.TimeoutException:
        logger.error(f"[Netease] 短链接解析超时 → {short_url}")
    except httpx.HTTPError as e:
        logger.error(f"[Netease] 短链接解析 HTTP 错误 → {short_url}: {e}")
    return None


async def extract_ids_from_text(
    text: str,
    card_data: list[dict[str, Any]] | None = None,
    *,
    timeout: int = 10,
    max_links: int = 5,
) -> dict[str, list[str]]:
    """从消息正文 + 卡片中提取所有网易云 ID（含短链接解析）。

    Returns:
        {"song": [...], "program": [...], "album": [...], "playlist": [...]}
    """
    result: dict[str, list[str]] = {"song": [], "program": [], "album": [], "playlist": []}
    urls = extract_all_urls(text, card_data)
    if not urls:
        return result

    short_urls: list[str] = []
    for url in urls:
        if extract_id_from_url(url, NETEASE_PROGRAM_URL_RE):
            _append_unique(result["program"], extract_id_from_url(url, NETEASE_PROGRAM_URL_RE))
            continue
        if extract_id_from_url(url, NETEASE_ALBUM_URL_RE):
            _append_unique(result["album"], extract_id_from_url(url, NETEASE_ALBUM_URL_RE))
            continue
        if extract_id_from_url(url, NETEASE_PLAYLIST_URL_RE):
            _append_unique(result["playlist"], extract_id_from_url(url, NETEASE_PLAYLIST_URL_RE))
            continue
        if extract_id_from_url(url, NETEASE_SONG_URL_RE):
            _append_unique(result["song"], extract_id_from_url(url, NETEASE_SONG_URL_RE))
            continue
        if NETEASE_SHORT_URL_RE.match(url):
            short_urls.append(url)

    for short_url in short_urls:
        resolved = await resolve_short_url(short_url, timeout=timeout)
        if not resolved:
            continue
        for key, pattern in (
            ("program", NETEASE_PROGRAM_URL_RE),
            ("album", NETEASE_ALBUM_URL_RE),
            ("playlist", NETEASE_PLAYLIST_URL_RE),
            ("song", NETEASE_SONG_URL_RE),
        ):
            value = extract_id_from_url(resolved, pattern)
            if value:
                _append_unique(result[key], value)
                break

    for key in result:
        result[key] = result[key][:max_links]
    return result


def _append_unique(items: list[str], value: Optional[str]) -> None:
    if value and value not in items:
        items.append(value)
