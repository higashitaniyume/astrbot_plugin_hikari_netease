"""
网易云音乐解析插件。

功能：
- 自动识别 music.163.com 歌曲/专辑/歌单/播客链接与 163cn.tv 短链接
- 私聊自动解析；群聊默认仅在被 @ 时解析（可在配置中开启 auto_parse_groups 指定群自动解析）
- 单曲/播客：下载音频后发送语音消息
- 专辑/歌单：批量下载后打包 ZIP 发送（仅私聊）
- 消息含 mp3 / flac 字样可切换音频格式偏好

移植自 HIKARI BOT NEO 的 netease_parser 插件。代码独立，不依赖任何宿主机器人模块。
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain, Record
from astrbot.api.star import Context, Star, register

try:
    from .api import (
        fetch_album_detail,
        fetch_playlist_detail,
        fetch_program_detail,
        fetch_song_detail,
        fetch_song_url,
    )
except ImportError:
    from api import (
        fetch_album_detail,
        fetch_playlist_detail,
        fetch_program_detail,
        fetch_song_detail,
        fetch_song_url,
    )
try:
    from .downloader import download_audio
except ImportError:
    from downloader import download_audio
try:
    from .packer import pack_to_zip
except ImportError:
    from packer import pack_to_zip
try:
    from .parser import (
        NeteaseSongInfo,
        extract_ids_from_text,
        has_netease_url,
    )
except ImportError:
    from parser import (
        NeteaseSongInfo,
        extract_ids_from_text,
        has_netease_url,
    )

# 消息中带 mp3 / flac 字样 → 指定格式（覆盖偏好）
_MP3_RE = re.compile(r"(?<![a-z])mp3(?![a-z])", re.I)
_FLAC_RE = re.compile(r"(?<![a-z])flac(?![a-z])", re.I)

# 触发正则：网易云链接 或 格式字样
_TRIGGER_RE = r"music\.163\.com|163cn\.tv|(?<![a-z])mp3(?![a-z])|(?<![a-z])flac(?![a-z])"

# 临时目录
if os.name == "nt":
    _TEMP_ROOT = Path(tempfile.gettempdir()) / "astrbot_netease"
else:
    _TEMP_ROOT = Path("/tmp/astrbot_netease")


def _sanitize_filename(text: str) -> str:
    """清理文件名中的非法字符。"""
    return "".join(c for c in text if c.isprintable() and c not in r'<>:"/\|?*').strip() or "netease"


@dataclass
class _ParseJob:
    """单个解析任务。"""

    event: AstrMessageEvent
    item_id: str
    item_type: str  # song / program / album / playlist
    quality: str  # auto / mp3 / flac
    user_id: str


class _Queue:
    """解析队列：串行 worker 消费，避免并发打爆磁盘/网络。"""

    def __init__(self, plugin: "NeteaseParserPlugin", delay_seconds: float = 0.8) -> None:
        self._plugin = plugin
        self._queue: asyncio.Queue[_ParseJob] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._delay = delay_seconds

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="AstrBotNeteaseQueue")
            logger.info("[Netease] 解析队列 worker 已启动")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def put(self, job: _ParseJob) -> None:
        self._queue.put_nowait(job)

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._plugin._process_job(job)
            except Exception:
                logger.exception(f"[Netease] 队列任务异常: {job.item_type} {job.item_id}")
            finally:
                self._queue.task_done()
            if self._delay > 0:
                await asyncio.sleep(self._delay)


@register("netease", "higashitaniyume", "网易云音乐解析：自动识别歌曲/专辑/歌单/播客链接，私聊发音频或 ZIP", "1.0.0")
class NeteaseParserPlugin(Star):
    """网易云音乐解析插件。"""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context, config)
        self.config = config
        self._queue: _Queue | None = None

    async def initialize(self) -> None:
        """启动解析队列 worker。"""
        cfg = self._cfg()
        settings = cfg.get("parse_queue") if isinstance(cfg.get("parse_queue"), dict) else {}
        if settings.get("enabled", True):
            self._queue = _Queue(self, delay_seconds=max(0.0, float(settings.get("delay_seconds", 0.8))))
            self._queue.start()

    async def terminate(self) -> None:
        if self._queue is not None:
            await self._queue.stop()
            self._queue = None

    # ── 配置工具 ──

    def _cfg(self) -> dict[str, Any]:
        return self.config or {}

    async def _send(self, event: AstrMessageEvent, text: str) -> None:
        """发送纯文本消息（AstrBot 的 event.send 要求 MessageChain）。"""
        await event.send(MessageChain([Plain(text)]))

    def _is_auto_parse_group(self, group_id: str) -> bool:
        auto = self._cfg().get("auto_parse_groups")
        if not isinstance(auto, dict):
            return False
        if not auto.get("enable", False):
            return False
        groups = [str(g) for g in auto.get("groups", []) if str(g)]
        return bool(group_id) and str(group_id) in groups

    async def _user_quality(self, user_id: str) -> str:
        """获取用户格式偏好（auto 表示未设置）。"""
        data = await self.get_kv_data("user_quality", {})
        if isinstance(data, dict):
            return str(data.get(user_id, "auto") or "auto")
        return "auto"

    async def _set_user_quality(self, user_id: str, quality: str) -> None:
        data = await self.get_kv_data("user_quality", {})
        if not isinstance(data, dict):
            data = {}
        data[user_id] = quality
        await self.put_kv_data("user_quality", data)

    # ── 入口 ──

    @filter.regex(_TRIGGER_RE)
    async def on_netease_message(self, event: AstrMessageEvent):
        """匹配网易云链接或 mp3/flac 字样"""
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return

        text = event.message_str or ""
        card_texts = _extract_card_texts(event)
        full_text = text + ("\n" + "\n".join(card_texts) if card_texts else "")

        # 无网易云链接 → 纯 mp3/flac 字样 → 设置格式偏好
        if not has_netease_url(full_text):
            async for result in self._handle_quality_switch(event, text):
                yield result
            return

        # 群聊策略：默认手动解析（仅被 @ 时），auto_parse_groups 指定群自动解析
        if event.get_message_type() == "group_message":
            group_id = event.get_group_id()
            at_wake = bool(getattr(event, "is_at_or_wake_command", False))
            if not (self._is_auto_parse_group(group_id) or at_wake):
                logger.info(f"[Netease] 群聊手动解析：group={group_id} at_or_wake={at_wake} → 跳过")
                return

        async for result in self._handle_links(event, full_text):
            yield result

    # ── 链接解析流程 ──

    async def _handle_links(self, event: AstrMessageEvent, full_text: str):
        cfg = self._cfg()
        max_links = max(1, int(cfg.get("max_links_per_message", 5)))
        ids = await extract_ids_from_text(
            full_text,
            timeout=int(cfg.get("short_link_timeout", 10)),
            max_links=max_links,
        )

        # 消息内带 mp3/flac 字样 → 本次解析按指定格式（覆盖偏好）
        if _MP3_RE.search(full_text) and not _FLAC_RE.search(full_text):
            quality = "mp3"
        elif _FLAC_RE.search(full_text) and not _MP3_RE.search(full_text):
            quality = "flac"
        else:
            quality = "auto"

        # 群聊中专辑/歌单仅提示私聊
        if (ids["album"] or ids["playlist"]) and event.get_message_type() == "group_message":
            yield event.plain_result("专辑/歌单请私聊发送，我会打包发给你～")
            return

        # 优先级：歌单 > 专辑 > 单曲/播客
        if ids["playlist"]:
            for pid in ids["playlist"]:
                await self._enqueue(event, pid, "playlist", quality)
            return
        if ids["album"]:
            for album_id in ids["album"]:
                await self._enqueue(event, album_id, "album", quality)
            return
        for pid in ids["program"]:
            await self._enqueue(event, pid, "program", quality)
        for sid in ids["song"]:
            await self._enqueue(event, sid, "song", quality)

    async def _enqueue(self, event: AstrMessageEvent, item_id: str, item_type: str, quality: str):
        job = _ParseJob(
            event=event,
            item_id=item_id,
            item_type=item_type,
            quality=quality,
            user_id=event.get_sender_id() or "",
        )
        if self._queue is not None:
            self._queue.put(job)
            logger.info(f"[Netease] 入队 → {item_type} {item_id} quality={quality}")
            return
        # 队列禁用 → 同步处理
        await self._process_job(job)

    # ── 任务处理 ──

    async def _process_job(self, job: _ParseJob) -> None:
        event = job.event
        cfg = self._cfg()
        api_base = str(cfg.get("api_base_url") or "").strip()
        if not api_base:
            await self._send(event, "未配置网易云 API 地址（api_base_url）")
            return
        real_ip = str(cfg.get("real_ip") or "")
        cookie = str(cfg.get("cookie") or "")
        timeout = int(cfg.get("api_timeout", 30))

        quality = job.quality
        if quality == "auto":
            quality = await self._user_quality(job.user_id)
        if quality == "auto":
            quality = str(cfg.get("default_quality", "flac"))
        high_quality = quality != "mp3"

        cache_dir = str(cfg.get("cache_dir") or _TEMP_ROOT / "audio")
        max_file_mb = int(cfg.get("max_file_mb", 200))

        try:
            if job.item_type == "song":
                await self._process_song(event, job.item_id, api_base, real_ip, cookie, timeout, high_quality, cache_dir, max_file_mb)
            elif job.item_type == "program":
                await self._process_program(event, job.item_id, api_base, real_ip, cookie, timeout, high_quality, cache_dir, max_file_mb)
            elif job.item_type == "album":
                await self._process_album(event, job.item_id, api_base, real_ip, cookie, timeout, high_quality, cache_dir, max_file_mb)
            elif job.item_type == "playlist":
                await self._process_playlist(event, job.item_id, api_base, real_ip, cookie, timeout, high_quality, cache_dir, max_file_mb)
        except ValueError as e:
            logger.warning(f"[Netease] {job.item_type} {job.item_id} 失败: {e}")
            await self._send(event, str(e))
        except Exception as e:
            logger.exception(f"[Netease] {job.item_type} {job.item_id} 处理异常: {e}")
            await self._send(event, f"解析失败：{type(e).__name__}")

    async def _process_song(self, event, song_id, api_base, real_ip, cookie, timeout, high_quality, cache_dir, max_file_mb) -> None:
        """单曲：下载音频 → 发语音消息。"""
        info = await fetch_song_detail(song_id, api_base, timeout, real_ip)
        await self._send(event, f"正在解析：{info.name} - {info.artist}")

        url_result = await fetch_song_url(song_id, api_base, timeout, real_ip, high_quality, cookie)
        if not url_result.url:
            await self._send(event, "音频不可用（可能需要版权/登录），可尝试在消息中附带 mp3 或 flac 切换格式")
            return
        ext = ".flac" if high_quality and url_result.type == "flac" else ".mp3"
        try:
            path = await download_audio(url_result.url, cache_dir, timeout, max_file_mb, file_ext=ext)
        except Exception as e:
            await self._send(event, f"音频下载失败：{e}")
            return
        try:
            await event.send(MessageChain([Record(file=str(path))]))
        finally:
            _try_cleanup(path)

    async def _process_program(self, event, program_id, api_base, real_ip, cookie, timeout, high_quality, cache_dir, max_file_mb) -> None:
        """播客节目：取 mainSong 音频 → 发语音消息。"""
        info = await fetch_program_detail(program_id, api_base, timeout, real_ip, cookie)
        await self._send(event, f"正在解析播客：{info.name} - {info.artist}")

        url_result = await fetch_song_url(info.id, api_base, timeout, real_ip, high_quality, cookie)
        if not url_result.url:
            await self._send(event, "音频不可用（可能需要版权/登录）")
            return
        ext = ".flac" if high_quality and url_result.type == "flac" else ".mp3"
        try:
            path = await download_audio(url_result.url, cache_dir, timeout, max_file_mb, file_ext=ext)
        except Exception as e:
            await self._send(event, f"音频下载失败：{e}")
            return
        try:
            await event.send(MessageChain([Record(file=str(path))]))
        finally:
            _try_cleanup(path)

    async def _process_album(self, event, album_id, api_base, real_ip, cookie, timeout, high_quality, cache_dir, max_file_mb) -> None:
        """专辑：批量下载 → ZIP 打包发送。"""
        album_name, songs = await fetch_album_detail(album_id, api_base, timeout, real_ip)
        await self._send(event, f"专辑「{album_name}」共 {len(songs)} 首，开始下载打包……")
        await self._download_and_pack(event, songs, album_name, api_base, real_ip, cookie, timeout, high_quality, cache_dir, max_file_mb)

    async def _process_playlist(self, event, playlist_id, api_base, real_ip, cookie, timeout, high_quality, cache_dir, max_file_mb) -> None:
        """歌单：批量下载 → ZIP 打包发送。"""
        playlist_name, songs = await fetch_playlist_detail(playlist_id, api_base, timeout, real_ip)
        await self._send(event, f"歌单「{playlist_name}」共 {len(songs)} 首，开始下载打包……")
        await self._download_and_pack(event, songs, playlist_name, api_base, real_ip, cookie, timeout, high_quality, cache_dir, max_file_mb)

    async def _download_and_pack(self, event, songs: list[NeteaseSongInfo], zip_name: str,
                                 api_base, real_ip, cookie, timeout, high_quality,
                                 cache_dir, max_file_mb) -> None:
        """批量下载歌曲并打包 ZIP 发送。"""
        cfg = self._cfg()
        max_songs = max(1, int(cfg.get("max_songs_per_zip", 50)))
        max_zip_mb = int(cfg.get("max_zip_mb", 200))

        files: list[tuple[Path, str]] = []
        failed = 0
        for index, song in enumerate(songs):
            try:
                url_result = await fetch_song_url(song.id, api_base, timeout, real_ip, high_quality, cookie)
                if not url_result.url:
                    failed += 1
                    continue
                ext = ".flac" if high_quality and url_result.type == "flac" else ".mp3"
                path = await download_audio(url_result.url, cache_dir, timeout, max_file_mb, file_ext=ext)
                arc_name = f"{index + 1:02d}. {_sanitize_filename(song.name)} - {_sanitize_filename(song.artist)}{ext}"
                files.append((path, arc_name))
            except Exception as e:
                failed += 1
                logger.warning(f"[Netease] 单曲下载失败 {song.id}: {e}")
            if len(files) >= max_songs:
                break

        if not files:
            await self._send(event, "没有成功下载任何歌曲")
            return

        output_dir = Path(str(cfg.get("pack_dir") or _TEMP_ROOT / "pack"))
        try:
            zip_paths = await pack_to_zip(
                files, _sanitize_filename(zip_name), output_dir,
                max_files=max_songs, max_size_mb=max_zip_mb,
            )
        except Exception as e:
            logger.exception(f"[Netease] ZIP 打包失败: {e}")
            await self._send(event, f"打包失败：{e}")
            return

        note = f"（{failed} 首下载失败）" if failed else ""
        await self._send(event, f"打包完成，共 {len(zip_paths)} 个 ZIP{note}")
        for zip_path in zip_paths:
            try:
                await event.send(MessageChain([File(name=zip_path.name, file=str(zip_path))]))
            except Exception as e:
                logger.warning(f"[Netease] ZIP 发送失败（可能平台不支持文件消息）: {e}")
                await self._send(event, f"ZIP 已生成但发送失败（当前平台可能不支持文件消息）：\n{zip_path}")
            finally:
                _try_cleanup(zip_path)

    # ── 格式偏好切换 ──

    async def _handle_quality_switch(self, event: AstrMessageEvent, text: str):
        cfg = self._cfg()
        if not cfg.get("quality_switch", True):
            return
        if _FLAC_RE.search(text) and not _MP3_RE.search(text):
            target = "flac"
        else:
            target = "mp3"
        user_id = event.get_sender_id() or ""
        await self._set_user_quality(user_id, target)
        label = "FLAC" if target == "flac" else "MP3"
        logger.info(f"[Netease] 格式偏好 → user={user_id} target={label}")
        yield event.plain_result(
            f"已记住你的偏好：以后解析默认发送 {label}（直接发链接即可；想换格式再说 mp3/flac）"
        )


# ── 工具 ──


def _extract_card_texts(event: AstrMessageEvent) -> list[str]:
    """从消息对象中提取卡片文本（QQ 音乐分享卡片等，尽力而为）。"""
    texts: list[str] = []
    try:
        message = event.get_message()
        for segment in getattr(message, "segments", []) or []:
            for field in ("json", "data", "content", "url"):
                value = getattr(segment, field, None)
                if isinstance(value, str) and "http" in value:
                    texts.append(value)
    except Exception:
        pass
    return texts


def _try_cleanup(path: Path) -> None:
    """删除已发送的临时文件（静默忽略错误）。"""
    try:
        if path.exists():
            path.unlink()
            logger.debug(f"[Netease] 已清理临时文件: {path.name}")
    except OSError as e:
        logger.warning(f"[Netease] 清理临时文件失败: {path} ({e})")
