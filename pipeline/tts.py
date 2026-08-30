# -*- coding: utf-8 -*-
"""Edge TTS 音频合成：每条选题一条 mp3，正文读 line1+line2。

合规话术每期只读两次：第 1 条开头的口播开场白（带产品名）+ 最后一条结尾。
音色用中文女声 zh-CN-XiaoxiaoNeural，语速调慢 -15%（老人听得更清）。
edge-tts 未安装或网络失败时优雅跳过并记录日志，不阻断其他产出。
"""
import asyncio
import logging
import re
from pathlib import Path

from config import DISCLAIMER

log = logging.getLogger(__name__)

VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "-15%"  # 语速调慢 10%~20%

# 口播开场白：只在第 1 条音频开头读一次（产品名 + 合规话术）
OPENING = f"这里是小公告翻译官，银发向。{DISCLAIMER}。"


def tts_text(item: dict, opening: bool = False, closing: bool = False) -> str:
    """单条朗读文本：line1 + line2；开场白/结尾合规话术只出现在首尾两条。"""
    parts = []
    if opening:
        parts.append(OPENING)
    parts += [item.get("line1", ""), item.get("line2", "")]
    if closing:
        parts.append(DISCLAIMER + "。")
    return " ".join(p for p in parts if p)


def _safe_name(company: str) -> str:
    return re.sub(r"[^\w一-鿿]+", "_", company or "未知")


async def _synth_one(text: str, out_path: Path, voice: str, rate: str):
    import edge_tts  # 延迟导入，未安装时由上层捕获
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(str(out_path))


async def _synth_all(items: list, out_dir: Path, voice: str, rate: str) -> tuple:
    paths, failures = [], []
    last = len(items)
    for i, item in enumerate(items, 1):
        out_path = out_dir / f"{i:02d}_{_safe_name(item.get('company'))}.mp3"
        try:
            await _synth_one(tts_text(item, opening=(i == 1), closing=(i == last)),
                             out_path, voice, rate)
            paths.append(out_path)
            log.info("TTS 成功：%s", out_path.name)
        except Exception as e:  # 单条失败不影响其他条
            failures.append((item.get("company"), str(e)))
            log.warning("TTS 失败（跳过 %s）：%s", out_path.name, e)
    return paths, failures


def synthesize(items: list, out_dir: Path, voice: str = VOICE, rate: str = RATE) -> tuple:
    """合成全部选题音频，返回 (成功路径列表, 失败清单)——失败不阻断发布。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        log.warning("edge-tts 未安装，跳过音频合成（pip install edge-tts 后可用）")
        return [], [(it.get("company"), "edge-tts 未安装") for it in items]
    try:
        return asyncio.run(_synth_all(items, out_dir, voice, rate))
    except Exception as e:  # 事件循环级失败同样优雅跳过
        log.warning("TTS 合成整体失败，跳过：%s", e)
        return [], [(it.get("company"), str(e)) for it in items]
