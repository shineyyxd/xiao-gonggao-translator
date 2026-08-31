# -*- coding: utf-8 -*-
"""Edge TTS 音频合成：口播稿（broadcast.py）→ 每条一个 mp3 + 整期合并一个 mp3。

口播文本规则全部在 broadcast.py（数字中文化保精度/绝不念链接长名/开场结尾各一次）。
音色 zh-CN-YunxiNeural（男声沉稳亲切），语速 -18%（老人听得更清）。
整期合并用 edge-tts 一次性合成整篇口播稿（停顿自然，优于 mp3 拼接）。
edge-tts 未安装或网络失败时优雅跳过并记录日志，不阻断其他产出。
"""
import asyncio
import logging
import re
from datetime import date as date_cls
from pathlib import Path

import broadcast

log = logging.getLogger(__name__)

VOICE = "zh-CN-YunxiNeural"
RATE = "-18%"  # 语速调慢 15%~20%


def _safe_name(company: str) -> str:
    return re.sub(r"[^\w一-鿿]+", "_", company or "未知")


async def _synth_one(text: str, out_path: Path, voice: str, rate: str):
    import edge_tts  # 延迟导入，未安装时由上层捕获
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(str(out_path))


async def _synth_all(items: list, out_dir: Path, voice: str, rate: str,
                     run_date: str) -> tuple:
    paths, failures = [], []
    # ① 每条一个 mp3（口播稿：第X条 + line1 + line2）
    for i, item in enumerate(items, 1):
        out_path = out_dir / f"{i:02d}_{_safe_name(item.get('company'))}.mp3"
        try:
            await _synth_one(broadcast.item_script(i, item), out_path, voice, rate)
            paths.append(out_path)
            log.info("TTS 成功：%s", out_path.name)
        except Exception as e:  # 单条失败不影响其他条
            failures.append((item.get("company"), str(e)))
            log.warning("TTS 失败（跳过 %s）：%s", out_path.name, e)
    # ② 整期合并一个 mp3（开场白+全部条+结尾，一次合成停顿自然）
    full_path = out_dir / f"整期_{run_date}.mp3"
    try:
        await _synth_one(broadcast.issue_script(run_date, items), full_path, voice, rate)
        paths.append(full_path)
        log.info("TTS 成功：%s", full_path.name)
    except Exception as e:
        failures.append(("整期合并", str(e)))
        log.warning("TTS 整期合并失败（跳过）：%s", e)
    return paths, failures


def synthesize(items: list, out_dir: Path, voice: str = VOICE, rate: str = RATE,
               run_date: str = None) -> tuple:
    """合成全部选题音频，返回 (成功路径列表, 失败清单)——失败不阻断发布。"""
    run_date = run_date or date_cls.today().isoformat()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        log.warning("edge-tts 未安装，跳过音频合成（pip install edge-tts 后可用）")
        return [], [(it.get("company"), "edge-tts 未安装") for it in items]
    try:
        return asyncio.run(_synth_all(items, out_dir, voice, rate, run_date))
    except Exception as e:  # 事件循环级失败同样优雅跳过
        log.warning("TTS 合成整体失败，跳过：%s", e)
        return [], [(it.get("company"), str(e)) for it in items]
