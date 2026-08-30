# -*- coding: utf-8 -*-
"""微信推送（Publisher）：企业微信群机器人 webhook（markdown 文本）。

- webhook 从环境变量 YFZB_WECOM_WEBHOOK 读取；未配置则优雅跳过。
- 推送失败重试 3 次（间隔 5s/15s/60s），仍失败写 `产出/<date>/待人工推送.txt`
  （含全文+原因），由 Orchestrator 记入 agent_calls。
- 停刊告警通道 send_alert()：复用 webhook（已配置时）发告警文本；
  未配置则只落盘+日志。
- 企业微信 markdown 单条消息有长度上限（约 4096 字节），超长自动分条发送。
"""
import logging
import time
from pathlib import Path

import requests

from config import DISCLAIMER, get_wecom_webhook

log = logging.getLogger(__name__)

_MAX_BYTES = 3500  # 留余量的单条上限
RETRY_INTERVALS = (5, 15, 60)  # 推送重试间隔（3 次）


def build_markdown(run_date: str, items: list) -> str:
    """大字版文字稿的 markdown 形态（与 HTML 同内容）。"""
    lines = [f"**小公告翻译官（银发向）{run_date}**", f"> {DISCLAIMER}", ""]
    for i, it in enumerate(items, 1):
        lines += [
            f"**{i}. {it['company']}**（{it['sector']}·{it['event_type']}）",
            it["line1"],
            it["line2"],
            it["line3"],
            f"[公告原文]({it['link']})",
            "",
        ]
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _chunks(text: str) -> list:
    """按字节上限把长文本切成若干条（按行切，避免截断句子）。"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len((cur + "\n" + line).encode("utf-8")) > _MAX_BYTES and cur:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def _post(webhook: str, payload: dict):
    resp = requests.post(webhook, json=payload, timeout=15)
    resp.raise_for_status()


def push_wecom(content: str, webhook: str = None, fallback_path: Path = None) -> dict:
    """推送 markdown 到企业微信群机器人，失败重试 3 次后落盘兜底。

    返回 {ok, attempts, error, skipped}。未配置 webhook 时 skipped=True。
    """
    webhook = webhook if webhook is not None else get_wecom_webhook()
    if not webhook:
        log.info("未配置 YFZB_WECOM_WEBHOOK，跳过微信推送")
        return {"ok": False, "attempts": 0, "error": "未配置 webhook", "skipped": True}
    attempts = 0
    for attempt in range(len(RETRY_INTERVALS) + 1):
        attempts = attempt + 1
        try:
            for part in _chunks(content):
                _post(webhook, {"msgtype": "markdown", "markdown": {"content": part}})
            return {"ok": True, "attempts": attempts, "error": "", "skipped": False}
        except requests.RequestException as e:
            log.warning("微信推送失败（第%d次）：%s", attempts, e)
            if attempt < len(RETRY_INTERVALS):
                time.sleep(RETRY_INTERVALS[attempt])
            else:
                error = str(e)
    # 重试耗尽：落盘待人工推送
    if fallback_path is not None:
        Path(fallback_path).write_text(
            f"微信推送失败（重试 {len(RETRY_INTERVALS) + 1} 次），请人工推送以下内容。\n"
            f"失败原因：{error}\n\n{content}", encoding="utf-8")
        log.error("推送重试耗尽，已落盘 %s", fallback_path)
    return {"ok": False, "attempts": attempts, "error": error, "skipped": False}


def send_alert(message: str, webhook: str = None, fallback_path: Path = None) -> bool:
    """停刊告警通道：复用 webhook 发告警文本；未配置则只落盘+日志。"""
    webhook = webhook if webhook is not None else get_wecom_webhook()
    log.error("停刊告警：%s", message)
    if fallback_path is not None:
        Path(fallback_path).write_text(f"【停刊告警】\n{message}\n", encoding="utf-8")
    if not webhook:
        log.warning("未配置 YFZB_WECOM_WEBHOOK，告警仅落盘")
        return False
    try:
        _post(webhook, {"msgtype": "text", "text": {"content": f"【小公告翻译官停刊告警】\n{message}"}})
        return True
    except requests.RequestException as e:
        log.error("告警发送失败：%s", e)
        return False
