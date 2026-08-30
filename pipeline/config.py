# -*- coding: utf-8 -*-
"""全局配置：路径常量 + 从环境变量读取的 LLM / 推送配置。

环境变量：
  生成模型  YFZB_GEN_API_KEY / YFZB_GEN_BASE_URL / YFZB_GEN_MODEL
  校对模型  YFZB_REV_API_KEY / YFZB_REV_BASE_URL / YFZB_REV_MODEL （缺省回退到 GEN 配置）
  微信推送  YFZB_WECOM_WEBHOOK （企业微信群机器人 webhook，未配置则跳过推送）
"""
import os
from pathlib import Path

# ---- 路径常量（pipeline/ 的上一级即项目根目录）----
PIPELINE_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """加载 pipeline/.env（若存在）到环境变量，已存在的变量不覆盖。

    本地开发免 export；.env 存密钥，不进任何版本库/产出目录。
    """
    env_path = PIPELINE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv()
PROJECT_ROOT = PIPELINE_DIR.parent
DATA_DIR = PROJECT_ROOT / "研发数据"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
OUTPUT_ROOT = PROJECT_ROOT / "产出"
DB_PATH = PIPELINE_DIR / "data" / "error_log.db"

# 研发样本的日期范围（数据层窗口过滤的边界说明见 README）
SAMPLE_START = "2026-08-22"
SAMPLE_END = "2026-08-26"

# 每期固定合规话术（开头结尾各出现一次，音频结尾也会读）
DISCLAIMER = "本内容仅为公告信息整理，不构成投资建议"

# LLM 默认配置
DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_GEN_MODEL = "kimi-k2-0905-preview"
DEFAULT_REV_MODEL = "kimi-k2-0905-preview"


def _env_temperature(name: str):
    """可选温度配置：不设置则返回 None（客户端不传该字段，用模型默认）。

    部分模型对温度有硬约束（实测 kimi-k3 只接受 temperature=1）。
    """
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def get_gen_config() -> dict:
    """生成 Agent 的模型配置（effort 为可选的思考档位约束，如 low/high/max）。"""
    return {
        "api_key": os.environ.get("YFZB_GEN_API_KEY", ""),
        "base_url": os.environ.get("YFZB_GEN_BASE_URL", DEFAULT_BASE_URL),
        "model": os.environ.get("YFZB_GEN_MODEL", DEFAULT_GEN_MODEL),
        "effort": os.environ.get("YFZB_GEN_EFFORT", ""),
        "temperature": _env_temperature("YFZB_GEN_TEMPERATURE"),
    }


def get_rev_config() -> dict:
    """校对 Agent 的模型配置（独立配置，缺省回退 GEN，避免'自己批自己作业'）。"""
    gen = get_gen_config()
    return {
        "api_key": os.environ.get("YFZB_REV_API_KEY") or gen["api_key"],
        "base_url": os.environ.get("YFZB_REV_BASE_URL") or gen["base_url"],
        "model": os.environ.get("YFZB_REV_MODEL") or DEFAULT_REV_MODEL,
        "effort": os.environ.get("YFZB_REV_EFFORT", ""),
        "temperature": _env_temperature("YFZB_REV_TEMPERATURE"),
    }


def get_wecom_webhook() -> str:
    return os.environ.get("YFZB_WECOM_WEBHOOK", "")


def get_prices() -> dict:
    """API 单价（元/百万 token），从 .env 读；默认空 = 日报只显示 token 数。

    单价请去平台后台查实时价格后填写，不要凭记忆编价格。
    """
    def _f(name):
        raw = os.environ.get(name, "").strip()
        try:
            return float(raw) if raw else None
        except ValueError:
            return None
    return {
        "writer_input": _f("YFZB_PRICE_GEN_INPUT_PER_MTOK"),
        "writer_output": _f("YFZB_PRICE_GEN_OUTPUT_PER_MTOK"),
        "checker_input": _f("YFZB_PRICE_REV_INPUT_PER_MTOK"),
        "checker_output": _f("YFZB_PRICE_REV_OUTPUT_PER_MTOK"),
    }


def prompt_version(path: Path) -> str:
    """prompt 文件内容的 sha256 前 8 位——skill 版本，记入 agent_calls 和 runs 表。"""
    import hashlib
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:8]
    except FileNotFoundError:
        return "missing"


def get_prompt_versions() -> tuple:
    """(生成 prompt 版本, 校对 prompt 版本)。"""
    return (prompt_version(PROMPTS_DIR / "生成Agent.md"),
            prompt_version(PROMPTS_DIR / "校对Agent.md"))
