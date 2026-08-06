"""
网易云音乐音频下载模块。

负责：
1. 从 api-enhanced 返回的音频 URL 下载到本地缓存
2. SHA256 哈希去重缓存
3. 流式下载并实时检查大小限制

移植自 HIKARI BOT NEO 的 netease_parser 插件。代码独立，不依赖任何宿主机器人模块。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path

import httpx

from astrbot.api import logger

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.7103.48 Safari/537.36"
)
CHUNK_LOG_INTERVAL_BYTES = 10 * 1024 * 1024  # 每 10MB 打印一次进度
_FLUSH_INTERVAL = 512 * 1024  # 512KB — 写入缓冲区阈值


def _flush_to_file(path: Path, data: bytes) -> None:
    """同步写入数据到文件（在后台线程中执行）。"""
    with open(path, "ab") as f:
        f.write(data)


def _cache_path(url: str, cache_dir: str, ext: str = ".mp3") -> Path:
    """根据 URL 生成缓存文件路径。"""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return Path(cache_dir) / f"netease_{digest[:16]}{ext}"


async def download_audio(
    url: str,
    cache_dir: str,
    timeout: int = 30,
    max_file_mb: int = 200,
    file_ext: str = ".mp3",
) -> Path:
    """下载音频文件到本地缓存（带重试，应对 CDN 断流）。

    Raises:
        RuntimeError: 下载失败或超过大小限制
        httpx.TimeoutException: 下载超时
    """
    path = _cache_path(url, cache_dir, file_ext)
    max_bytes = max(int(max_file_mb), 1) * 1024 * 1024
    max_retries = 2  # 最多重试 2 次（共 3 次尝试）

    # 缓存命中
    if path.exists() and path.stat().st_size > 0:
        if path.stat().st_size > max_bytes:
            raise RuntimeError(
                f"缓存音频超过大小限制：{path.stat().st_size / 1024 / 1024:.1f}MB"
            )
        logger.info(f"[Netease] 缓存命中 → {path.name} ({path.stat().st_size / 1024 / 1024:.1f}MB)")
        return path

    # 下载（带重试）
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 2):
        t_start = time.time()
        logger.info(f"[Netease] 开始下载 (第 {attempt}/{max_retries + 1} 次) → {url[:120]}")

        headers = {"User-Agent": USER_AGENT}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0, read=timeout, pool=timeout),
            follow_redirects=True,
        ) as client:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_suffix = f".part.{os.getpid()}.{id(path)}"
            tmp_path = path.with_suffix(path.suffix + tmp_suffix)
            try:
                async with client.stream("GET", url, headers=headers) as resp:
                    resp.raise_for_status()

                    cl = resp.headers.get("content-length")
                    remote_size = int(cl) if cl and cl.isdigit() else 0
                    if remote_size > max_bytes:
                        raise RuntimeError(
                            f"音频超过大小限制：{remote_size / 1024 / 1024:.1f}MB"
                        )

                    written = 0
                    last_chunk_log = 0
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        buf.extend(chunk)
                        written += len(chunk)
                        if written > max_bytes:
                            raise RuntimeError(
                                f"音频超过大小限制：{written / 1024 / 1024:.1f}MB"
                            )
                        if len(buf) >= _FLUSH_INTERVAL:
                            await asyncio.to_thread(_flush_to_file, tmp_path, bytes(buf))
                            buf.clear()
                        if written - last_chunk_log >= CHUNK_LOG_INTERVAL_BYTES:
                            last_chunk_log = written
                            elapsed_now = time.time() - t_start
                            logger.info(
                                f"[Netease] 下载中... {written / 1024 / 1024:.1f}MB / "
                                f"{remote_size / 1024 / 1024:.1f}MB ({elapsed_now:.1f}s)"
                            )
                    if buf:
                        await asyncio.to_thread(_flush_to_file, tmp_path, bytes(buf))
                        buf.clear()

                if path.exists():
                    tmp_path.unlink(missing_ok=True)
                    logger.debug(f"[Netease] 下载跳过 → 文件已被其他协程写入: {path.name}")
                else:
                    tmp_path.replace(path)
                tmp_path = None
                break  # 成功

            except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                last_error = e
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
                if attempt <= max_retries:
                    wait = attempt * 2.0
                    logger.warning(f"[Netease] 下载失败 (第 {attempt} 次), {wait:.0f}s 后重试: {e}")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"[Netease] 下载失败 (第 {attempt} 次), 已无重试次数: {e}")
                raise
            except Exception:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
                if not path.exists():
                    raise

    if not path.exists():
        raise RuntimeError("下载完成但文件不存在（并发 rename 异常）")

    logger.info(f"[Netease] 下载完成 → {path.name} ({path.stat().st_size / 1024 / 1024:.1f}MB)")
    return path
