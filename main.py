"""
网易云音乐解析插件。

功能：
- 自动识别 music.163.com 歌曲/专辑/歌单/播客链接与 163cn.tv 短链接（含 QQ 卡片与引用回复）
- 私聊发链接/卡片即解析；群聊默认仅「@bot + 链接」或「@bot + 引用卡片」时解析
  （可在配置中开启 auto_parse_groups 指定群自动解析）
- 单曲/播客：下载音频后发送；专辑/歌单：批量下载后打包 ZIP（仅私聊）
- 默认发最高音质；改音质唯一入口为「回复机器人消息 + mp3/flac」（更新偏好并重发）
- 群聊收到网易云链接但未 @ 时，回一句引导提示（带冷却）

移植自 HIKARI BOT NEO 的 netease_parser 插件。代码独立，不依赖任何宿主机器人模块。
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.message_components import File, Json, Music, Plain, Reply, Share
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

# 消息中带 mp3 / flac 字样（仅用于「回复换格式」场景）
_MP3_RE = re.compile(r"(?<![a-z])mp3(?![a-z])", re.I)
_FLAC_RE = re.compile(r"(?<![a-z])flac(?![a-z])", re.I)

# 临时目录
if os.name == "nt":
    _TEMP_ROOT = Path(tempfile.gettempdir()) / "astrbot_netease"
else:
    _TEMP_ROOT = Path("/tmp/astrbot_netease")

# 回复换格式时，重发记录的时间窗（秒）
_RECENT_WINDOW_SECONDS = 30 * 60


def _sanitize_filename(text: str) -> str:
    """清理文件名中的非法字符。"""
    return "".join(c for c in text if c.isprintable() and c not in r'<>:"/\|?*').strip() or "netease"


# ── 组件 URL 提取工具 ──


def _json_urls(data: Any) -> list[str]:
    """递归从 JSON 卡片数据中提取所有含 http 的字符串。"""
    urls: list[str] = []
    if isinstance(data, dict):
        for value in data.values():
            urls.extend(_json_urls(value))
    elif isinstance(data, list):
        for item in data:
            urls.extend(_json_urls(item))
    elif isinstance(data, str) and "http" in data:
        urls.append(data)
    return urls


def _component_urls(seg: Any) -> list[str]:
    """从单个消息组件提取可能的 URL（QQ 音乐卡片 / 分享卡片 / JSON 卡片）。"""
    urls: list[str] = []
    if isinstance(seg, Music):
        for field_name in ("url", "audio"):
            value = getattr(seg, field_name, None)
            if isinstance(value, str) and "http" in value:
                urls.append(value)
    elif isinstance(seg, Share):
        value = getattr(seg, "url", None)
        if isinstance(value, str) and "http" in value:
            urls.append(value)
    elif isinstance(seg, Json):
        urls.extend(_json_urls(getattr(seg, "data", None)))
    return urls


def _component_has_netease(seg: Any) -> bool:
    """组件中是否含网易云链接。"""
    return any(has_netease_url(u) for u in _component_urls(seg))


def _collect_card_urls(segments: list) -> list[str]:
    """从消息组件列表中提取所有卡片 URL。"""
    urls: list[str] = []
    for seg in segments:
        urls.extend(_component_urls(seg))
    return urls


def _find_reply(segments: list) -> Reply | None:
    """从消息组件列表中查找第一个引用回复组件。"""
    for seg in segments:
        if isinstance(seg, Reply):
            return seg
    return None


def _reply_urls(reply_seg: Reply) -> list[str]:
    """提取被引用消息中的 URL（正文 + 卡片）。"""
    urls: list[str] = []
    if isinstance(getattr(reply_seg, "message_str", None), str) and reply_seg.message_str:
        urls.append(reply_seg.message_str)
    for comp in (getattr(reply_seg, "chain", None) or []):
        urls.extend(_component_urls(comp))
    return urls


# ── 触发 Filter ──


class NeteaseTriggerFilter(CustomFilter):
    """自定义触发器：覆盖网易云链接（正文/卡片/引用）与 mp3/flac 字样。

    相比 @filter.regex，这里能访问完整消息链，从而识别 QQ 音乐卡片
    与「@bot + 引用卡片」这类 message_str 里没有链接的场景。
    """

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        try:
            text = event.message_str or ""
            if _MP3_RE.search(text) or _FLAC_RE.search(text):
                return True
            if has_netease_url(text):
                return True
            for seg in event.get_messages():
                if isinstance(seg, Reply):
                    if seg.message_str and has_netease_url(seg.message_str):
                        return True
                    for comp in (seg.chain or []):
                        if _component_has_netease(comp):
                            return True
                elif _component_has_netease(seg):
                    return True
        except Exception:
            pass
        return False


@dataclass
class _ParseJob:
    """单个解析任务。"""

    event: AstrMessageEvent
    item_id: str
    item_type: str  # song / program / album / playlist
    quality: str  # auto / mp3 / flac
    user_id: str


@dataclass
class _SentRecord:
    """一次发送记录，用于「回复 mp3/flac 换格式重发」。"""

    item_type: str  # song / program / album / playlist
    item_id: str
    title: str
    quality: str  # flac / mp3（实际格式）
    sent_at: float = field(default_factory=time.time)


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


@register("netease", "higashitaniyume", "网易云音乐解析：自动识别歌曲/专辑/歌单/播客链接，私聊发音频或 ZIP", "1.1.0")
class NeteaseParserPlugin(Star):
    """网易云音乐解析插件。"""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context, config)
        self.config = config
        self._queue: _Queue | None = None
        self._recent: dict[str, list[_SentRecord]] = {}
        self._card_hint_last: dict[str, float] = {}

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

    # ── 发送记录（回复换格式重发用） ──

    def _record_send(self, user_id: str, item_type: str, item_id: str, title: str, quality: str) -> None:
        if not user_id:
            return
        rec = _SentRecord(item_type=item_type, item_id=item_id, title=title, quality=quality)
        lst = self._recent.setdefault(user_id, [])
        lst.insert(0, rec)
        del lst[5:]
        logger.info(f"[Netease] 记录发送 → user={user_id} type={item_type} id={item_id} quality={quality}")

    def _find_recent(self, user_id: str) -> _SentRecord | None:
        """查找该用户最近一条时间窗内的发送记录。"""
        if not user_id:
            return None
        now = time.time()
        for rec in self._recent.get(user_id, []):
            if now - rec.sent_at <= _RECENT_WINDOW_SECONDS:
                return rec
        return None

    # ── 入口 ──

    @filter.custom_filter(NeteaseTriggerFilter)
    async def on_netease_message(self, event: AstrMessageEvent):
        """网易云链接 / 卡片 / 引用 / mp3-flac 触发的主入口。"""
        cfg = self._cfg()
        if not cfg.get("enabled", True):
            return

        text = event.message_str or ""
        segments = list(event.get_messages())
        reply_seg = _find_reply(segments)
        card_urls = _collect_card_urls(segments)

        own_has_link = has_netease_url(text) or any(has_netease_url(u) for u in card_urls)

        reply_urls = _reply_urls(reply_seg) if reply_seg is not None else []
        reply_has_link = any(has_netease_url(u) for u in reply_urls)

        has_mp3_flac = bool(_MP3_RE.search(text) or _FLAC_RE.search(text))

        # 1) 回复换格式：回复机器人消息 + mp3/flac（无链接）
        if has_mp3_flac and reply_seg is not None and not own_has_link and not reply_has_link:
            if str(reply_seg.sender_id) == str(event.get_self_id()):
                async for result in self._handle_quality_reply(event, text):
                    yield result
                event.stop_event()
            return

        # 2) 解析：自身链接或引用链接
        if own_has_link or reply_has_link:
            is_group = bool(event.get_group_id())
            is_tome = bool(event.is_at_or_wake_command)

            # 群聊非白名单、未 @ → 卡片引导（带冷却）
            if is_group and not is_tome and not self._is_auto_parse_group(event.get_group_id()):
                if self._card_hint_ready(event.get_group_id()):
                    self._mark_card_hint(event.get_group_id())
                    yield event.plain_result("🎵 想下载这首歌？引用这条卡片并 @我 即可")
                return

            full_text = text
            extra_urls = card_urls + reply_urls
            if extra_urls:
                joined = "\n".join(extra_urls)
                full_text = f"{full_text}\n{joined}" if full_text else joined

            async for result in self._handle_links(event, full_text):
                yield result
            event.stop_event()
            return

    # ── 卡片引导冷却 ──

    def _card_hint_ready(self, group_id: str) -> bool:
        hint = self._cfg().get("card_hint")
        if not isinstance(hint, dict) or not hint.get("enabled", True):
            return False
        cooldown = max(0.0, float(hint.get("cooldown_seconds", 300)))
        last = self._card_hint_last.get(str(group_id), 0.0)
        return (time.monotonic() - last) >= cooldown

    def _mark_card_hint(self, group_id: str) -> None:
        self._card_hint_last[str(group_id)] = time.monotonic()

    # ── 回复换格式 ──

    async def _handle_quality_reply(self, event: AstrMessageEvent, text: str):
        cfg = self._cfg()
        if not cfg.get("quality_switch", True):
            return
        if _FLAC_RE.search(text) and not _MP3_RE.search(text):
            target = "flac"
        else:
            target = "mp3"
        user_id = event.get_sender_id() or ""
        label = target.upper()
        rec = self._find_recent(user_id)
        logger.info(f"[Netease] 回复换格式 → user={user_id} target={label} hit={rec is not None}")

        if rec is None:
            await self._set_user_quality(user_id, target)
            yield event.plain_result(f"已记住你的偏好：以后解析默认发 {label}（直接发链接即可）")
            return
        if rec.quality == target:
            yield event.plain_result(f"这条已经是 {label} 版了～")
            return
        await self._set_user_quality(user_id, target)
        yield event.plain_result(f"已改默认音质为 {label}，正在重新发送～")
        await self._enqueue(event, rec.item_id, rec.item_type, target)

    # ── 链接解析流程 ──

    async def _handle_links(self, event: AstrMessageEvent, full_text: str):
        cfg = self._cfg()
        max_links = max(1, int(cfg.get("max_links_per_message", 5)))
        ids = await extract_ids_from_text(
            full_text,
            timeout=int(cfg.get("short_link_timeout", 10)),
            max_links=max_links,
        )

        # 音质由用户偏好决定（改音质只走回复换格式）
        quality = "auto"

        # 群聊中专辑/歌单仅提示私聊
        if (ids["album"] or ids["playlist"]) and bool(event.get_group_id()):
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
        """单曲：下载音频 → 发送。"""
        info = await fetch_song_detail(song_id, api_base, timeout, real_ip)
        await self._send(event, f"正在解析：{info.name} - {info.artist}")

        url_result = await fetch_song_url(song_id, api_base, timeout, real_ip, high_quality, cookie)
        if not url_result.url:
            await self._send(event, "音频不可用（可能需要版权/登录）")
            return
        ext = ".flac" if url_result.type == "flac" else ".mp3"
        try:
            path = await download_audio(url_result.url, cache_dir, timeout, max_file_mb, file_ext=ext)
        except Exception as e:
            await self._send(event, f"音频下载失败：{e}")
            return
        display_name = f"{_sanitize_filename(info.name)} - {_sanitize_filename(info.artist)}{ext}"
        await self._send_audio(event, path, display_name)
        self._record_send(
            event.get_sender_id() or "", "song", song_id, info.name,
            "flac" if url_result.type == "flac" else "mp3",
        )

    async def _process_program(self, event, program_id, api_base, real_ip, cookie, timeout, high_quality, cache_dir, max_file_mb) -> None:
        """播客节目：取 mainSong 音频 → 发送。"""
        info = await fetch_program_detail(program_id, api_base, timeout, real_ip, cookie)
        await self._send(event, f"正在解析播客：{info.name} - {info.artist}")

        url_result = await fetch_song_url(info.id, api_base, timeout, real_ip, high_quality, cookie)
        if not url_result.url:
            await self._send(event, "音频不可用（可能需要版权/登录）")
            return
        ext = ".flac" if url_result.type == "flac" else ".mp3"
        try:
            path = await download_audio(url_result.url, cache_dir, timeout, max_file_mb, file_ext=ext)
        except Exception as e:
            await self._send(event, f"音频下载失败：{e}")
            return
        display_name = f"{_sanitize_filename(info.name)} - {_sanitize_filename(info.artist)}{ext}"
        await self._send_audio(event, path, display_name)
        self._record_send(
            event.get_sender_id() or "", "program", program_id, info.name,
            "flac" if url_result.type == "flac" else "mp3",
        )

    async def _send_audio(self, event, path: Path, display_name: str) -> None:
        """发送音频：优先经 OneBot 上传接口以 base64 发送（跨容器可用），失败时降级为文件消息。"""
        try:
            try:
                await self._upload_onebot_file(event, path, display_name)
                return
            except Exception as e:
                logger.warning(f"[Netease] OneBot 文件上传失败，降级为文件消息: {e}")
            try:
                await event.send(MessageChain([File(name=display_name, file=str(path))]))
            except Exception as e:
                logger.warning(f"[Netease] 文件消息发送失败（可能平台不支持）: {e}")
                await self._send(event, f"音频已下载但发送失败（当前平台可能不支持文件消息）：\n{display_name}")
        finally:
            _try_cleanup(path)

    async def _upload_onebot_file(self, event: AstrMessageEvent, path: Path, name: str) -> None:
        """通过 OneBot upload_*_file 接口上传文件（base64，兼容协议端与 AstrBot 分离部署）。"""
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            raise RuntimeError("当前平台不支持 OneBot 文件上传")
        raw = await asyncio.to_thread(path.read_bytes)
        payload = f"base64://{base64.b64encode(raw).decode()}"
        group_id = event.get_group_id()
        if group_id and str(group_id).isdigit():
            await bot.call_action("upload_group_file", group_id=int(group_id), file=payload, name=name)
        else:
            user_id = event.get_sender_id()
            if not user_id or not str(user_id).isdigit():
                raise RuntimeError("无法获取有效的用户 ID")
            await bot.call_action("upload_private_file", user_id=int(user_id), file=payload, name=name)

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
                ext = ".flac" if url_result.type == "flac" else ".mp3"
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
                try:
                    await self._upload_onebot_file(event, zip_path, zip_path.name)
                    continue
                except Exception as e:
                    logger.warning(f"[Netease] OneBot ZIP 上传失败，降级为文件消息: {e}")
                try:
                    await event.send(MessageChain([File(name=zip_path.name, file=str(zip_path))]))
                except Exception as e:
                    logger.warning(f"[Netease] ZIP 发送失败（可能平台不支持文件消息）: {e}")
                    await self._send(event, f"ZIP 已生成但发送失败（当前平台可能不支持文件消息）：\n{zip_path}")
            finally:
                _try_cleanup(zip_path)


# ── 工具 ──


def _try_cleanup(path: Path) -> None:
    """删除已发送的临时文件（静默忽略错误）。"""
    try:
        if path.exists():
            path.unlink()
            logger.debug(f"[Netease] 已清理临时文件: {path.name}")
    except OSError as e:
        logger.warning(f"[Netease] 清理临时文件失败: {path} ({e})")
