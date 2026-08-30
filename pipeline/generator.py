# -*- coding: utf-8 -*-
"""生成 Agent：公告 → 人话稿（三段式 line1/line2/line3）。

关键架构结论（首期实测）：announcement_link 严禁给 LLM，
prompt 只给标题 + 结构化字段 + 官方摘要；链接由管线层在最终产出时
从数据字段直接注入（见 main.py），机制性消除 A 类出处错误。

mock 模式（--mock-llm 或无 API key）：
  - 公司名能在 研发数据/人话稿_v2_0826.json 匹配上的，直接返回该文件的
    line1/line2/line3（修复版样例，全部通过核查）；
  - 匹配不上的用模板从结构化字段拼：数字一律原样引用摘要子串，不编造。
"""
import ast
import json
import logging
import re

from compliance import find_banned
from config import DATA_DIR, PROMPTS_DIR, DISCLAIMER

log = logging.getLogger(__name__)

GOLDEN_PATH = DATA_DIR / "人话稿_v2_0826.json"


def load_system_prompt() -> str:
    """system prompt 直接读 prompts/生成Agent.md。"""
    return (PROMPTS_DIR / "生成Agent.md").read_text(encoding="utf-8")


def load_golden(path=GOLDEN_PATH) -> list:
    """加载修复版人话稿样例（输出格式标准 + mock 数据源）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("未找到 golden 样例 %s，mock 将全部走模板", path)
        return []


def clean_title(ann: dict) -> str:
    """去掉标题的'证券简称:'前缀。"""
    title = ann.get("info_title") or ""
    abbr = ann.get("secu_abbr") or ""
    if abbr and title.startswith(abbr + ":"):
        return title[len(abbr) + 1:]
    return title.split(":", 1)[-1] if ":" in title[:12] else title


def parse_event_fields(ann: dict) -> dict:
    """解析 info_event_txt（Python 字面量风格字符串，单引号，尾部可能带事件名）。

    用 ast.literal_eval 且容错：解析失败返回 {}。
    """
    raw = (ann.get("info_event_txt") or "").strip()
    if not raw:
        return {}
    # 尾部常带一个事件名尾巴（如 "]股份回购"），截到最后一个 ']'
    end = raw.rfind("]")
    candidate = raw[: end + 1] if end != -1 else raw
    try:
        data = ast.literal_eval(candidate)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
    except (SyntaxError, ValueError):
        pass
    return {}


def _cn_date(iso_date: str) -> str:
    """2026-08-26 → 2026年8月26日"""
    try:
        y, m, d = iso_date[:10].split("-")
        return f"{int(y)}年{int(m)}月{int(d)}日"
    except (ValueError, IndexError):
        return iso_date


def build_user_message(ann: dict, feedback: list = None) -> str:
    """构造生成用用户消息：标题 + 结构化字段 + 摘要（严禁包含链接）。"""
    payload = {
        "公司": ann.get("secu_abbr"),
        "公告标题": clean_title(ann),
        "发布日期": ann.get("info_publ_date"),
        "事件类型": ann.get("event_type"),
        "板块": ann.get("sector"),  # 管线判定的行业分类，供"（做XX的）"使用
        "事件结构化字段": parse_event_fields(ann).get("关键信息", {}),
        "官方摘要": ann.get("info_summary"),
    }
    msg = (
        "请把下面这条公告改写成三段式人话稿。\n"
        "只输出 JSON：{\"line1\": \"...\", \"line2\": \"...\", \"line3\": \"...\"}\n"
        "line1=一句话说事（≤40字），line2=跟你有啥关系（≤80字），line3=原文出处（公告名称+发布日期）。\n"
        f"公告信息：\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    if feedback:
        msg += (
            "\n\n上一版被校对打回，错误清单如下，请逐条改正后重新输出：\n"
            + json.dumps(feedback, ensure_ascii=False, indent=2)
        )
    return msg


def parse_draft(text: str) -> dict:
    """从模型输出解析 line1/line2/line3。优先 JSON，退化到按行解析。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if all(obj.get(k) for k in ("line1", "line2", "line3")):
                return {k: str(obj[k]).strip() for k in ("line1", "line2", "line3")}
        except json.JSONDecodeError:
            pass
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) >= 3:
        return {"line1": lines[0], "line2": lines[1], "line3": lines[2]}
    raise ValueError(f"无法解析生成输出：{text[:120]}")


# ---- Writer 输出契约（格式不符判打回，记 conclusion=CONTRACT_FAIL）----
CONTRACT_LINE1_MAX = 50  # 铁律 ≤40 字，契约宽限到 50

# 合规话术只许出现在每期开头/结尾（管线统一注入），正文三段里不许自带——
# k3 会自行在稿尾加"不构成投资建议"，此处机制性拦截（老人反馈：全文出现5次太吵）。
DISCLAIMER_FORBIDDEN_FRAGMENT = "不构成投资建议"


def validate_contract(draft: dict) -> list:
    """输出契约校验：严格 {line1, line2, line3}、line1 ≤50 字、无禁词、
    line1/line2/line3 不含合规话术（话术由管线在期首/期尾统一注入）。

    返回问题清单（空 = 通过）。契约不过视同一次失败重试，不进 precheck/校对。
    """
    problems = []
    if not isinstance(draft, dict):
        return ["输出不是 JSON 对象"]
    for key in ("line1", "line2", "line3"):
        if not isinstance(draft.get(key), str) or not draft.get(key, "").strip():
            problems.append(f"缺少非空字段 {key}")
    extra = set(draft) - {"line1", "line2", "line3"}
    if extra:
        problems.append(f"多出字段 {sorted(extra)}")
    line1 = draft.get("line1") or ""
    if len(line1) > CONTRACT_LINE1_MAX:
        problems.append(f"line1 超长（{len(line1)} 字 > {CONTRACT_LINE1_MAX}）")
    for key in ("line1", "line2", "line3"):
        if DISCLAIMER_FORBIDDEN_FRAGMENT in (draft.get(key) or ""):
            problems.append(f"{key} 含合规话术「{DISCLAIMER_FORBIDDEN_FRAGMENT}」"
                            "（话术由管线统一加在期首/期尾，正文不要写）")
    banned = find_banned("".join(draft.get(k, "") for k in ("line1", "line2", "line3")))
    if banned:
        problems.append(f"含禁词 {banned}")
    return problems


def mock_generate(ann: dict, golden: list) -> dict:
    """确定性 mock：golden 样例按公司名命中则直接返回，否则走模板。"""
    abbr = ann.get("secu_abbr") or ""
    for g in golden:
        if g.get("company") == abbr:
            return {"line1": g["line1"], "line2": g["line2"], "line3": g["line3"]}
    return template_generate(ann)


def template_generate(ann: dict) -> dict:
    """模板兜底：从结构化字段/摘要原句拼装，数字均为原文子串，不编造。"""
    abbr = ann.get("secu_abbr") or "该公司"
    title = clean_title(ann)
    short = re.sub(r"的公告$", "", title)

    line1 = f"{abbr}发布公告：{short}。"
    if len(line1) > 40:
        line1 = line1[:39] + "。"

    # line2 取摘要前两句（摘要原句自带原值数字）；含禁词的句子跳过
    sentences = [s for s in re.split(r"(?<=。)", (ann.get("info_summary") or "").strip()) if s]
    picked = []
    for s in sentences:
        if find_banned(s):
            continue
        picked.append(s)
        if sum(len(x) for x in picked) >= 80:
            break
    line2 = "".join(picked)[:80] or "详见公告原文。"

    line3 = f"《{title}》，{_cn_date(ann.get('info_publ_date') or '')}发布。"
    return {"line1": line1, "line2": line2, "line3": line3}


class Generator:
    """生成 Agent：真实模式走 LLM，mock 模式走确定性生成。"""

    def __init__(self, client=None, mock: bool = False, golden: list = None):
        self.client = client
        self.mock = mock or client is None
        self.system_prompt = load_system_prompt()
        self.golden = golden if golden is not None else load_golden()
        # 最近一次真实调用的 token 用量（mock 或无调用时为 None），由编排层读取
        self.last_usage = None

    def generate(self, ann: dict, feedback: list = None) -> dict:
        """生成一版人话稿；feedback 为校对打回的错误清单（重试时传入）。"""
        if self.mock:
            # mock 是确定性的，重试结果相同，由上层控制重试次数
            self.last_usage = None
            return mock_generate(ann, self.golden)
        user = build_user_message(ann, feedback)
        text = self.client.chat(self.system_prompt, user)
        self.last_usage = self.client.last_usage
        return parse_draft(text)
