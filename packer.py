"""
网易云音乐多文件打包模块。

将下载好的多首歌曲打包为 ZIP（支持按文件数/大小拆分）。

移植自 HIKARI BOT NEO 的 netease_parser 插件。代码独立，不依赖任何宿主机器人模块。
"""

from __future__ import annotations

import asyncio
import os
import time
import zipfile
from pathlib import Path

from astrbot.api import logger


def _sanitize_arcname(text: str) -> str:
    """清理 ZIP 内文件名中的非法字符。"""
    return "".join(c for c in text if c.isprintable() and c not in r'<>:"/\|?*').strip()


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f}MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes}B"


def _create_zip_sync(zip_path: Path, batch_files: list, max_size_bytes: int) -> int:
    """同步创建 ZIP 并写入文件列表（在后台线程执行）。"""
    current_size = 0
    written_count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src_path_raw, arc_name in batch_files:
            src_path = Path(src_path_raw)
            if not src_path.exists():
                logger.warning(f"[Netease] 打包跳过: 文件不存在 → {src_path.name}")
                continue
            arc_name_clean = _sanitize_arcname(arc_name) or src_path.name
            file_size = src_path.stat().st_size
            if current_size + file_size > max_size_bytes:
                logger.warning(
                    f"[Netease] 打包跳过（超大小限制）→ {arc_name_clean} "
                    f"({_format_size(current_size)} + {_format_size(file_size)} > {max_size_bytes // (1024 * 1024)}MB)"
                )
                continue
            zf.write(src_path, arc_name_clean)
            current_size += file_size
            written_count += 1
    return written_count


async def pack_to_zip(
    files: list[tuple[Path, str]],
    zip_name: str,
    output_dir: str | Path,
    max_files: int = 50,
    max_size_mb: int = 200,
) -> list[Path]:
    """将多个文件打包为 ZIP（支持拆分）。

    Returns:
        ZIP 文件路径列表（可能因拆分有多个）
    """
    if not files:
        raise ValueError("没有文件需要打包")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    max_size_bytes = max_size_mb * 1024 * 1024
    sanitized_base = _sanitize_arcname(zip_name) or "netease_album"
    zip_paths: list[Path] = []

    total_files = len(files)
    batch_size = max(1, min(max_files, total_files))

    for batch_idx in range(0, total_files, batch_size):
        batch = files[batch_idx:batch_idx + batch_size]
        part_suffix = f".part{batch_idx // batch_size + 1}" if total_files > batch_size else ""
        zip_filename = f"{sanitized_base}{part_suffix}.zip"
        zip_path = output_path / zip_filename
        temp_zip = zip_path.with_suffix(f".zip.tmp.{os.getpid()}")

        logger.info(f"[Netease] 开始打包 → {zip_filename} ({len(batch)} 个文件)")

        try:
            written_count = await asyncio.to_thread(_create_zip_sync, temp_zip, batch, max_size_bytes)
            if not written_count:
                temp_zip.unlink(missing_ok=True)
                logger.warning(f"[Netease] 打包结果为空 → {zip_filename}")
                continue
            if zip_path.exists():
                temp_zip.unlink(missing_ok=True)
            else:
                temp_zip.replace(zip_path)
            zip_paths.append(zip_path)
            logger.info(f"[Netease] 打包完成 → {zip_path.name} ({_format_size(zip_path.stat().st_size)})")
        except Exception:
            temp_zip.unlink(missing_ok=True)
            logger.exception(f"[Netease] 打包失败 → {zip_filename}")
            raise

        await asyncio.sleep(0.1)  # 避免同一目录写冲突

    if not zip_paths:
        raise RuntimeError("打包失败：未生成任何有效的 ZIP 文件")
    return zip_paths
