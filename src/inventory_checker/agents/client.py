"""Chat client factory.

PRD: "Any model that reads images runs in the firm's approved environment, with no client
data used for training." The default is therefore Azure OpenAI in the firm's tenant, signed
in with Entra ID (DefaultAzureCredential) or an API key. Plain OpenAI is supported for local
development only. With nothing configured, AI assist is off and the app still works.
"""

from __future__ import annotations

import os
from typing import Any


def model_name() -> str | None:
    return os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("OPENAI_MODEL")


def build_chat_client() -> Any | None:
    from agent_framework.openai import OpenAIChatCompletionClient

    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    if endpoint and deployment:
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
    if os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_MODEL"):
        return OpenAIChatCompletionClient(os.environ["OPENAI_MODEL"], api_key=os.environ["OPENAI_API_KEY"])
    return None
