"""Model providers, all used through Microsoft Agent Framework (MAF) chat clients.

Choose one with ``AI_PROVIDER``; if it is unset, the first provider whose settings are present wins,
in this order:

========================  ===========================================  ==================================
``AI_PROVIDER``           Settings                                     MAF client
========================  ===========================================  ==================================
``anthropic``             ``ANTHROPIC_API_KEY``, ``CLAUDE_MODEL``      ``agent_framework_anthropic``
                          (default ``claude-haiku-5-5``)               ``AnthropicClient``
``azure_openai``          ``AZURE_OPENAI_ENDPOINT``,                   ``agent_framework.openai``
                          ``AZURE_OPENAI_DEPLOYMENT``, and               ``OpenAIChatCompletionClient``
                          ``AZURE_OPENAI_API_KEY`` or Entra ID         (``azure_endpoint=...``)
``openai``                ``OPENAI_API_KEY``, ``OPENAI_MODEL``         ``OpenAIChatCompletionClient``
``none``                  -                                            detection off
========================  ===========================================  ==================================

Any vision-capable chat model works; the deployment you pick for Azure OpenAI or OpenAI must accept
images. Add another provider with :func:`register_provider`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_CLAUDE_MODEL = "claude-haiku-5-5"


@dataclass(frozen=True)
class Provider:
    name: str
    is_configured: Callable[[], bool]
    build: Callable[[], Any]  # returns a MAF chat client
    model: Callable[[], str]


_REGISTRY: dict[str, Provider] = {}


def register_provider(provider: Provider) -> None:
    """Add or replace a provider. Registration order is the auto-detection order."""
    _REGISTRY[provider.name] = provider


def registered_providers() -> list[str]:
    return list(_REGISTRY)


# ----------------------------------------------------------------------------- Anthropic (Claude)


def _anthropic_client_class() -> type:
    from agent_framework_anthropic import AnthropicClient

    class ClaudeClient(AnthropicClient):
        """Anthropic client with cost-conscious defaults: effort ``low`` unless ``CLAUDE_EFFORT`` says otherwise."""

        def _prepare_options(self, messages: Sequence[Any], options: Mapping[str, Any], request_state: Any = None,
                             /, **kwargs: Any) -> dict[str, Any]:
            opts = dict(options)
            output_config = dict(opts.get("output_config") or {})
            output_config.setdefault("effort", os.getenv("CLAUDE_EFFORT", "low"))
            opts["output_config"] = output_config
            return super()._prepare_options(messages, opts, request_state, **kwargs)

    return ClaudeClient


def build_anthropic(*, api_key: str | None = None, base_url: str | None = None, model: str | None = None) -> Any:
    kwargs: dict[str, Any] = {"model": model or os.getenv("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL),
                              "api_key": api_key or os.environ["ANTHROPIC_API_KEY"]}
    if base_url:
        kwargs["base_url"] = base_url
    return _anthropic_client_class()(**kwargs)


register_provider(Provider(
    name="anthropic",
    is_configured=lambda: bool(os.getenv("ANTHROPIC_API_KEY")),
    build=build_anthropic,
    model=lambda: os.getenv("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL),
))

# ----------------------------------------------------------------------------- Azure OpenAI


def build_azure_openai(*, endpoint: str | None = None, deployment: str | None = None, api_key: str | None = None,
                       api_version: str | None = None) -> Any:
    from agent_framework.openai import OpenAIChatCompletionClient

    endpoint = endpoint or os.environ["AZURE_OPENAI_ENDPOINT"]
    deployment = deployment or os.environ["AZURE_OPENAI_DEPLOYMENT"]
    api_version = api_version or os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    api_key = api_key or os.getenv("AZURE_OPENAI_API_KEY")
    if api_key:
        return OpenAIChatCompletionClient(deployment, api_key=api_key, azure_endpoint=endpoint, api_version=api_version)
    try:  # keyless: Entra ID (managed identity, az login, workload identity, ...)
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise RuntimeError("Set AZURE_OPENAI_API_KEY, or install the 'azure' extra (pip install "
                           "'inventory-checker-agent[azure]') to sign in with Entra ID") from exc
    return OpenAIChatCompletionClient(deployment, credential=DefaultAzureCredential(), azure_endpoint=endpoint,
                                      api_version=api_version)


register_provider(Provider(
    name="azure_openai",
    is_configured=lambda: bool(os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_DEPLOYMENT")),
    build=build_azure_openai,
    model=lambda: os.getenv("AZURE_OPENAI_DEPLOYMENT", ""),
))

# ----------------------------------------------------------------------------- OpenAI


def build_openai(*, api_key: str | None = None, model: str | None = None, base_url: str | None = None) -> Any:
    from agent_framework.openai import OpenAIChatCompletionClient

    kwargs: dict[str, Any] = {"api_key": api_key or os.environ["OPENAI_API_KEY"]}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAIChatCompletionClient(model or os.environ["OPENAI_MODEL"], **kwargs)


register_provider(Provider(
    name="openai",
    is_configured=lambda: bool(os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL")),
    build=build_openai,
    model=lambda: os.getenv("OPENAI_MODEL", ""),
))

# ----------------------------------------------------------------------------- selection


def selected_provider() -> Provider | None:
    name = os.getenv("AI_PROVIDER", "").strip().lower().replace("-", "_")
    if name == "none":
        return None
    if name:
        if name not in _REGISTRY:
            raise ValueError(f"Unknown AI_PROVIDER {name!r}; choose one of {', '.join(_REGISTRY)} or none")
        return _REGISTRY[name]
    return next((p for p in _REGISTRY.values() if p.is_configured()), None)


def build_chat_client() -> tuple[Any, str, str] | None:
    """(MAF chat client, provider name, model) for the selected provider, or None when none is configured."""
    provider = selected_provider()
    if provider is None:
        return None
    return provider.build(), provider.name, provider.model()
