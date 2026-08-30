# -*- coding: utf-8 -*-
"""校对 Agent：对照原文逐点核查，输出"通过/打回 + 错误清单"。

错误分类：A数字错 / B无中生有 / C倾向性话术 / D遗漏关键限定。
校对使用独立模型配置（config.get_rev_config），避免"自己批自己作业"。

mock 模式：直接用 precheck 的确定性检查结果作为校对结论
（数字原值核对 → A 类；禁词 → C 类）。
"""
import json
import logging
import re

import precheck
from config import PROMPTS_DIR

log = logging.getLogger(__name__)


def load_system_prompt() -> str:
    """system prompt 直接读 prompts/校对Agent.md。"""
    return (PROMPTS_DIR / "校对Agent.md").read_text(encoding="utf-8")


def build_user_message(ann: dict, draft: dict, extra_context: str = None) -> str:
    body = {
        "公告标题": ann.get("info_title"),
        "发布日期": ann.get("info_publ_date"),
        "官方摘要": ann.get("info_summary"),
        "事件结构化字段": ann.get("info_event_txt"),
        "人话稿": {
            "line1": draft.get("line1"),
            "line2": draft.get("line2"),
            "line3": draft.get("line3"),
        },
    }
    msg = (
        "请按检查项逐项核对，输出结论与错误清单。\n"
        f"{json.dumps(body, ensure_ascii=False, indent=2)}"
    )
    if extra_context:
        # Memory/预检给的核查提示（历史高频错误分布、无锚点分句）——只给 Checker
        msg += f"\n\n{extra_context}"
    return msg


def parse_verdict(text: str) -> dict:
    """解析校对输出：结论：通过/打回 + 错误清单（JSON 数组，可缺省）。

    输出契约：无法解析出明确结论时按"打回"处理（宁可错杀），parse_ok=False。
    结论行先查全文（推理模型可能在结论前输出分析过程），再退回前三行判断；
    "打回"优先于"通过"判断（避免'不通过'类表述误判）。
    """
    text = text or ""
    # 全文找规范结论行："结论：通过" / "结论：打回"（k3 实测格式）
    m_concl = re.search(r"结论[:：]\s*(通过|打回|不通过)", text)
    if m_concl:
        word = m_concl.group(1)
        passed = word == "通过"
        parse_ok = True
    else:
        head = text.split("\n", 3)[0:3]
        head_text = "\n".join(head)
        parse_ok = ("打回" in head_text) or ("通过" in head_text) or ("结论" in head_text)
        passed = ("打回" not in head_text) and ("通过" in head_text or "结论" in head_text)
    errors = []
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                errors = [e for e in arr if isinstance(e, dict)]
        except json.JSONDecodeError:
            pass
    if not passed and not errors:
        note = "校对输出无法解析，按打回处理（宁可错杀）" if not parse_ok else "校对打回（未给出结构化清单）"
        errors = [{"类型": "其他", "位置": "", "问题": note}]
    return {"passed": passed, "errors": errors, "parse_ok": parse_ok}


class Reviewer:
    """校对 Agent：真实模式走独立 LLM，mock 模式用 precheck 确定性结果。"""

    def __init__(self, client=None, mock: bool = False):
        self.client = client
        self.mock = mock or client is None
        self.system_prompt = load_system_prompt()
        # 最近一次真实调用的 token 用量（mock 或无调用时为 None），由编排层读取
        self.last_usage = None

    def review(self, ann: dict, draft: dict, extra_context: str = None) -> dict:
        """返回 {passed: bool, errors: [...], parse_ok: bool}。

        extra_context：Memory 的历史高频错误分布 + precheck 无锚点分句清单
        （只进 Checker 上下文，绝不进 Writer——Memory 铁律）。
        """
        if self.mock:
            self.last_usage = None
            pre = precheck.check(draft, ann)
            return {"passed": pre["ok"], "errors": pre["errors"], "parse_ok": True}
        user = build_user_message(ann, draft, extra_context)
        text = self.client.chat(self.system_prompt, user)
        self.last_usage = self.client.last_usage
        return parse_verdict(text)
