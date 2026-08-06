"""
网易云音乐 API 调用模块。

调用 api-enhanced 服务器（自建网易云 API 服务）：
- /song/detail        歌曲信息
- /song/url           音频下载链接
- /dj/program/detail  播客节目信息
- /album              专辑详情
- /playlist/detail    歌单详情

移植自 HIKARI BOT NEO 的 netease_parser 插件。代码独立，不依赖任何宿主机器人模块。
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

import httpx

from astrbot.api import logger

try:
    from .parser import USER_AGENT, NeteaseSongInfo, NeteaseSongUrlResult
except ImportError:
    from parser import USER_AGENT, NeteaseSongInfo, NeteaseSongUrlResult


def _api_url(api_base: str, path: str) -> str:
    """构建完整的 API URL。"""
    base = api_base.rstrip("/")
    if not base.startswith("http"):
        base = f"http://{base}"
    return f"{base}{path}"


async def _request_json(url: str, timeout: int = 30) -> dict:
    """GET 请求并解析 JSON，统一异常处理。"""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    t_start = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.error(f"[Netease] API 超时 ({time.time() - t_start:.1f}s) → {url}")
        raise
    except httpx.HTTPStatusError as e:
        logger.error(f"[Netease] API HTTP 错误 ({time.time() - t_start:.1f}s) → {url} HTTP {e.response.status_code}")
        raise
    except Exception:
        logger.error(f"[Netease] API 请求异常 ({time.time() - t_start:.1f}s) → {url}")
        raise

    if not isinstance(data, dict) or data.get("code") != 200:
        code = data.get("code") if isinstance(data, dict) else "?"
        msg = data.get("msg", "") if isinstance(data, dict) else ""
        raise ValueError(f"API 返回异常: code={code}, msg={msg}")
    return data


def _parse_song(s: dict) -> NeteaseSongInfo:
    """解析歌曲 dict（兼容 ar/al 与 artists/album 两种字段名）。"""
    artists = s.get("ar") or s.get("artists") or []
    artist_names = " / ".join(a.get("name", "") for a in artists if isinstance(a, dict))
    album_info = s.get("al") or s.get("album") or {}
    album_name = album_info.get("name", "") if isinstance(album_info, dict) else ""
    return NeteaseSongInfo(
        id=str(s.get("id", "")),
        name=str(s.get("name", "")),
        artist=artist_names,
        album=album_name,
        pic_url=album_info.get("picUrl", "") if isinstance(album_info, dict) else "",
    )


async def fetch_song_detail(song_id: str, api_base: str, timeout: int = 30, real_ip: str = "") -> NeteaseSongInfo:
    """获取歌曲详细信息。"""
    path = f"/song/detail?ids={song_id}"
    if real_ip:
        path += f"&realIP={real_ip}"
    data = await _request_json(_api_url(api_base, path), timeout)

    songs = data.get("songs", [])
    if not songs:
        raise ValueError("歌曲不存在")
    return _parse_song(songs[0])


async def fetch_song_url(
    song_id: str,
    api_base: str,
    timeout: int = 30,
    real_ip: str = "",
    high_quality: bool = True,
    cookie: str = "",
) -> NeteaseSongUrlResult:
    """获取歌曲音频下载 URL。

    high_quality=True → br=999000（FLAC > 320k > 192k）
    high_quality=False → br=320000（320kbps MP3）
    """
    path = f"/song/url?id={song_id}"
    path += "&br=999000" if high_quality else "&br=320000"
    if real_ip:
        path += f"&realIP={real_ip}"
    if cookie:
        path += f"&cookie={quote(cookie)}"
    data = await _request_json(_api_url(api_base, path), timeout)

    items = data.get("data", [])
    if not items:
        return NeteaseSongUrlResult(code=404)
    item = items[0]
    return NeteaseSongUrlResult(
        url=str(item.get("url") or ""),
        br=int(item.get("br", 0)),
        size=int(item.get("size", 0)),
        type=str(item.get("type", "mp3")),
        code=int(item.get("code", 200)),
    )


async def fetch_program_detail(
    program_id: str,
    api_base: str,
    timeout: int = 30,
    real_ip: str = "",
    cookie: str = "",
) -> NeteaseSongInfo:
    """获取播客/电台节目详细信息（音频实际是 mainSong）。"""
    path = f"/dj/program/detail?id={program_id}"
    if real_ip:
        path += f"&realIP={real_ip}"
    if cookie:
        path += f"&cookie={quote(cookie)}"
    data = await _request_json(_api_url(api_base, path), timeout)

    program = data.get("program")
    if not program:
        raise ValueError("播客节目不存在")
    main_song = program.get("mainSong") or {}
    song_id = str(main_song.get("id", ""))
    name = str(main_song.get("name", program.get("name", "")) or "")
    artists_list = main_song.get("artists") or program.get("artists") or []
    artist_names = " / ".join(a.get("name", "") for a in artists_list if isinstance(a, dict))
    radio = program.get("radio") or {}
    return NeteaseSongInfo(
        id=song_id,
        name=name,
        artist=artist_names,
        album=str(radio.get("name", "") or ""),
    )


async def fetch_album_detail(
    album_id: str,
    api_base: str,
    timeout: int = 30,
    real_ip: str = "",
) -> tuple[str, list[NeteaseSongInfo]]:
    """获取专辑详情，返回 (专辑名, 曲目列表)。"""
    path = f"/album?id={album_id}"
    if real_ip:
        path += f"&realIP={real_ip}"
    data = await _request_json(_api_url(api_base, path), timeout)

    songs_raw = data.get("songs", [])
    if not songs_raw:
        raise ValueError(f"专辑不存在或为空 (id={album_id})")
    first = songs_raw[0]
    al = first.get("al") or {}
    album_name = str(al.get("name", "")) if isinstance(al, dict) else ""
    songs = [_parse_song(s) for s in songs_raw]
    return album_name, songs


async def fetch_playlist_detail(
    playlist_id: str,
    api_base: str,
    timeout: int = 30,
    real_ip: str = "",
) -> tuple[str, list[NeteaseSongInfo]]:
    """获取歌单详情，返回 (歌单名, 曲目列表)。"""
    path = f"/playlist/detail?id={playlist_id}"
    if real_ip:
        path += f"&realIP={real_ip}"
    data = await _request_json(_api_url(api_base, path), timeout)

    playlist_data = data.get("playlist")
    if not playlist_data:
        raise ValueError(f"歌单不存在或为空 (id={playlist_id})")
    playlist_name = str(playlist_data.get("name", ""))
    songs_raw = playlist_data.get("tracks", [])
    if not songs_raw:
        raise ValueError(f"歌单为空 (id={playlist_id})")
    return playlist_name, [_parse_song(s) for s in songs_raw]
