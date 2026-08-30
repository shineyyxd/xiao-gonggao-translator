# -*- coding: utf-8 -*-
"""OpenAI 兼容 API 客户端（生成 / 校对各持有一个独立实例）。

mock 模式不在本模块实现：生成端与校对端的确定性 mock 分别在
generator.py / reviewer.py 中按业务语义实现。
"""
import os
import time

import requests


class LLMClient:
    # 全局限速（跨实例共享：Writer/Checker 同属一个账号，RPM 按组织计）
    _last_call_ts = 0.0
    # Kimi 免费档实测组织级 RPM=3 → 默认每次调用间隔 ≥21s；可用环境变量覆盖
    MIN_INTERVAL = float(os.environ.get("YFZB_MIN_CALL_INTERVAL", "21"))
    RATE_LIMIT_RETRIES = 5

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 0,
                 effort: str = "", temperature=None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        # 单次调用超时：推理模型长思考实测 >90s（Kimi k2.6/k3），默认放宽到 300s
        self.timeout = timeout or int(os.environ.get("YFZB_LLM_TIMEOUT", "300"))
        # 思考档位约束（如 kimi-k3 支持 low/high/max）：Harness 对思考链的预算控制
        self.effort = effort
        # 温度：None = 不传该字段用模型默认（部分模型如 kimi-k3 只接受 temperature=1）
        self.temperature = temperature
        # 最近一次调用的 token 用量（供 agent_calls 计量留痕）；无调用时为 None
        self.last_usage = None

    def chat(self, system: str, user: str, temperature: float = 0.2, max_tokens: int = 8000) -> str:
        """单次对话补全，返回 assistant 文本内容。失败抛 requests 异常。

        max_tokens 默认 8000：推理模型会先消耗数千 reasoning token 再给答案，
        默认 2000 会被思考耗尽导致 content 为空；finish_reason=length 时显式
        报错交由上层重试。配置 effort 时向 API 传 reasoning_effort 约束思考预算。
        每次调用后 self.last_usage 记录 {prompt_tokens, completion_tokens,
        reasoning_tokens}（reasoning 含在 completion 内，属输出计费）。
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        temp = self.temperature if self.temperature is not None else temperature
        if temp is not None:
            payload["temperature"] = temp
        if self.effort:
            payload["reasoning_effort"] = self.effort
        for attempt in range(self.RATE_LIMIT_RETRIES + 1):
            # 全局限速：距上次调用不足 MIN_INTERVAL 则等待（组织级 RPM=3 实测）
            wait = self.MIN_INTERVAL - (time.time() - LLMClient._last_call_ts)
            if wait > 0:
                time.sleep(wait)
            LLMClient._last_call_ts = time.time()
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            if resp.status_code == 429 and attempt < self.RATE_LIMIT_RETRIES:
                # 限流：等一个限速周期再重试（Harness 吸收平台波动，不让业务感知）
                time.sleep(max(self.MIN_INTERVAL, float(resp.headers.get("Retry-After", 21))))
                continue
            break
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        self.last_usage = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": details.get("reasoning_tokens"),
        }
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError(f"模型输出被 max_tokens={max_tokens} 截断（推理模型思考占用过多）")
        return choice["message"]["content"]
