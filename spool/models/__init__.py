"""Model gateway: provider-neutral types plus adapters.

`make_model(spec)` turns a short spec into a client:
  local                      llama-server on SPOOL_LOCAL_BASE_URL (default http://127.0.0.1:8089/v1)
  openai:<model>             api.openai.com, OPENAI_API_KEY
  dashscope:<model>          Qwen on DashScope (OpenAI-compatible mode), DASHSCOPE_API_KEY
  deepseek:<model>           DEEPSEEK_API_KEY
  moonshot:<model>           MOONSHOT_API_KEY
  openrouter:<model>         OPENROUTER_API_KEY
  compat:<model>@<base_url>  any OpenAI-compatible server, SPOOL_API_KEY
  anthropic:<model>          Messages API via the official SDK (ANTHROPIC_API_KEY or an `ant` profile)
  script:<file.json>         a scripted agent for tests and demos: the k-th assistant turn of every
                             session is the k-th item, [{"content": "...", "tool_calls": [{"name", "arguments"}]}]
"""

from __future__ import annotations

import os

from .base import ModelClient, ModelError, ModelRequest, ModelResponse, parse_arguments
from .openai_compat import OpenAICompatClient

__all__ = [
    "ModelClient",
    "ModelError",
    "ModelRequest",
    "ModelResponse",
    "parse_arguments",
    "make_model",
]

_HOSTED = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "moonshot": ("https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
}


def make_model(spec: str) -> ModelClient:
    if spec == "local":
        return OpenAICompatClient(
            base_url=os.environ.get("SPOOL_LOCAL_BASE_URL", "http://127.0.0.1:8089/v1"),
            model=os.environ.get("SPOOL_LOCAL_MODEL", "local"),
            name="local",
            price_key="local",
            slot_param="id_slot",
            default_slot=int(os.environ["SPOOL_SLOT"]) if os.environ.get("SPOOL_SLOT") else None,
            # Qwen3.5 thinks by default; on a laptop that multiplies latency. Sampling follows
            # Qwen's recommendation for non-thinking mode (temperature comes from the config).
            extra_body={"chat_template_kwargs": {"enable_thinking": False}, "cache_prompt": True,
                        "top_p": 0.8, "top_k": 20},
        )
    provider, _, rest = spec.partition(":")
    if not rest:
        raise ValueError(f"model spec must look like provider:model, got {spec!r}")
    if provider == "script":
        import json

        from .scripted import ByStepModel, calls, reply

        items = json.loads(open(rest, encoding="utf-8").read())
        script = [calls([(c["name"], c.get("arguments") or {}, c.get("id")) for c in it["tool_calls"]],
                        text=it.get("content", "")) if it.get("tool_calls") else reply(it.get("content", ""))
                  for it in items]
        return ByStepModel(script, name=spec)
    if provider == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(model=rest)
    if provider == "compat":
        model, _, base_url = rest.partition("@")
        if not base_url:
            raise ValueError("compat spec must look like compat:<model>@<base_url>")
        return OpenAICompatClient(base_url=base_url, model=model, api_key=os.environ.get("SPOOL_API_KEY"), name=spec)
    if provider in _HOSTED:
        base_url, key_env = _HOSTED[provider]
        key = os.environ.get(key_env)
        if not key:
            raise ModelError(f"{key_env} is not set")
        return OpenAICompatClient(base_url=base_url, model=rest, api_key=key, name=spec)
    raise ValueError(f"unknown model provider {provider!r}")
