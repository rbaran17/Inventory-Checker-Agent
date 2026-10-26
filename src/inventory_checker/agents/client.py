"""Chat client factory.

PRD: "Any model that reads images runs in the firm's approved environment, with no client
data used for training." Pick the provider with ``AI_PROVIDER``:

- ``claude``: Anthropic Claude via the Claude API (``ANTHROPIC_API_KEY``). Model ``CLAUDE_MODEL``,
  default ``claude-opus-5-5``.
- ``claude-foundry``: Claude in the firm's Azure tenant via Microsoft Foundry
  (``ANTHROPIC_FOUNDRY_RESOURCE`` and ``ANTHROPIC_FOUNDRY_API_KEY``).
- ``azure``: Azure OpenAI (``AZURE_OPENAI_ENDPOINT``, ``AZURE_OPENAI_DEPLOYMENT``), signed in with an
  API key or Entra ID (DefaultAzureCredential).
- ``openai``: plain OpenAI, for local development only.
- unset: the first one that is configured, in the order above.

With nothing configured, AI assist is off and the app still works.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
# The MAF connector defaults max_tokens to 1024, too short for plans and memo summaries.
CLAUDE_MAX_TOKENS = 16000
# Refusal fallback: on a safety decline the Claude API re-runs the request on a suitable model.
CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _claude_defaults(options: Mapping[str, Any], *, server_fallback: bool) -> dict[str, Any]:
    opts = dict(options)
    if not opts.get("max_tokens"):
        opts["max_tokens"] = int(os.getenv("CLAUDE_MAX_TOKENS", CLAUDE_MAX_TOKENS))
    output_config = dict(opts.get("output_config") or {})
    # Claude Opus 5.5 defaults to "medium"; set it explicitly so behaviour is stable across models.
    output_config.setdefault("effort", os.getenv("CLAUDE_EFFORT", "medium"))
    opts["output_config"] = output_config
    if server_fallback:
        opts.setdefault("fallbacks", "default")
        opts["additional_beta_flags"] = [*opts.get("additional_beta_flags", []), CLAUDE_FALLBACK_BETA]
    return opts


def _claude_client_classes() -> tuple[type, type]:
    from agent_framework_anthropic import AnthropicClient, AnthropicFoundryClient

    class ClaudeClient(AnthropicClient):
        """Claude API client with request defaults suited to this app."""

        def _prepare_options(self, messages: Sequence[Any], options: Mapping[str, Any], request_state: Any = None,
                             /, **kwargs: Any) -> dict[str, Any]:
            return super()._prepare_options(messages, _claude_defaults(options, server_fallback=True),
                                            request_state, **kwargs)

    class ClaudeFoundryClient(AnthropicFoundryClient):
        """Claude on Microsoft Foundry. Server-side refusal fallback is not available there."""

        def _prepare_options(self, messages: Sequence[Any], options: Mapping[str, Any], request_state: Any = None,
                             /, **kwargs: Any) -> dict[str, Any]:
            return super()._prepare_options(messages, _claude_defaults(options, server_fallback=False),
                                            request_state, **kwargs)

    return ClaudeClient, ClaudeFoundryClient


def provider() -> str | None:
    chosen = os.getenv("AI_PROVIDER", "").strip().lower()
    if chosen:
        return None if chosen == "none" else chosen
    if os.getenv("ANTHROPIC_API_KEY"):
        return "claude"
    if os.getenv("ANTHROPIC_FOUNDRY_RESOURCE"):
        return "claude-foundry"
    if os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_DEPLOYMENT"):
        return "azure"
    if os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL"):
        return "openai"
    return None


def model_name() -> str | None:
    p = provider()
    if p in ("claude", "claude-foundry"):
        return os.getenv("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)
    if p == "azure":
        return os.getenv("AZURE_OPENAI_DEPLOYMENT")
    if p == "openai":
        return os.getenv("OPENAI_MODEL")
    return None


def build_chat_client() -> Any | None:
    p = provider()
    if p is None:
        return None
    if p == "claude":
        claude_cls, _ = _claude_client_classes()
        return claude_cls(model=model_name(), api_key=os.getenv("ANTHROPIC_API_KEY"))
    if p == "claude-foundry":
        _, foundry_cls = _claude_client_classes()
        return foundry_cls(model=model_name(), resource=os.getenv("ANTHROPIC_FOUNDRY_RESOURCE"),
                           api_key=os.getenv("ANTHROPIC_FOUNDRY_API_KEY"))

    from agent_framework.openai import OpenAIChatCompletionClient

    if p == "azure":
        endpoint, deployment = os.environ["AZURE_OPENAI_ENDPOINT"], os.environ["AZURE_OPENAI_DEPLOYMENT"]
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
        api_key = os.getenv("AZURE_OPENAI_API_KEY")
        if api_key:
            return OpenAIChatCompletionClient(
                deployment, api_key=api_key, azure_endpoint=endpoint, api_version=api_version
            )
        from azure.identity import DefaultAzureCredential  # optional extra: pip install .[azure]

        return OpenAIChatCompletionClient(
            deployment, credential=DefaultAzureCredential(), azure_endpoint=endpoint, api_version=api_version
        )
    if p == "openai":
        return OpenAIChatCompletionClient(os.environ["OPENAI_MODEL"], api_key=os.environ["OPENAI_API_KEY"])
    raise ValueError(f"Unknown AI_PROVIDER {p!r} (use claude, claude-foundry, azure, openai or none)")
