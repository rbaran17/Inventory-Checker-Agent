# Model providers

Detection runs as a [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) (MAF) `Agent`.
The agent doesn't care which model it talks to: every provider is a MAF **chat client**, built in
[`src/inventory_checker/providers.py`](../src/inventory_checker/providers.py).

| `AI_PROVIDER` | MAF package / client | Required settings |
|---|---|---|
| `anthropic` | `agent-framework-anthropic` → `AnthropicClient` | `ANTHROPIC_API_KEY` |
| `azure_openai` | `agent-framework-openai` → `OpenAIChatCompletionClient(azure_endpoint=…)` | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, plus `AZURE_OPENAI_API_KEY` *or* Entra ID |
| `openai` | `agent-framework-openai` → `OpenAIChatCompletionClient` | `OPENAI_API_KEY`, `OPENAI_MODEL` |
| `none` | — | Detection off. The UI still loads |

If `AI_PROVIDER` is empty, the first provider in the table whose settings are present is used. The laptop
page header and the `serve` banner show which provider and model are active.

Whatever you choose, the model must accept **images**: use a vision-capable model or deployment.

## How one request looks

Each scan (one photo, or a video segment of up to 6 frames) is **one** chat request:

1. System instructions: how to identify, count and match objects (`detection.INSTRUCTIONS`).
2. One user turn with the frames as image parts, followed by the client population as JSON.
3. A strict JSON-schema answer format (`detection.RESPONSE_FORMAT`). It follows the strictest common
   subset of both APIs' structured-output rules: every property is required, every object sets
   `additionalProperties: false`, nullable fields use `anyOf`, and there are no numeric limits.
   Ranges such as `0 ≤ confidence ≤ 1` are enforced when the answer is parsed.
4. A SKU the model returns that isn't in the population is dropped, with a note.

The tests in `tests/test_detection.py` replay this against local fakes of the Anthropic Messages API, the
OpenAI Chat Completions API and Azure OpenAI. The fakes reject the schemas the real APIs reject.

## Anthropic Claude

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export CLAUDE_MODEL=claude-haiku-5-5   # default; Claude's lowest-cost current model
export CLAUDE_EFFORT=low               # low | medium | high | xhigh | max
```

- Get a key at <https://platform.claude.com>. The account needs credits.
- `CLAUDE_EFFORT` trades thoroughness for tokens. `low` is plenty for counting desk objects. Try
  `medium` if counts on busy shelves are unreliable.
- A larger model, for example `CLAUDE_MODEL=claude-sonnet-5-5`, is more accurate on dense or
  hard-to-read scenes, at a higher price.
- MAF's `agent_framework_anthropic` also has `AnthropicFoundryClient` (Claude on Microsoft Foundry),
  `AnthropicBedrockClient` and `AnthropicVertexClient`. Add them with `register_provider` (see
  [extending.md](extending.md)) if you need Claude inside your cloud tenant.

## Azure OpenAI

```bash
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
export AZURE_OPENAI_DEPLOYMENT=<your vision-capable chat deployment>
export AZURE_OPENAI_API_KEY=...          # or leave unset to use Entra ID
export AZURE_OPENAI_API_VERSION=2024-10-21
```

**Keyless (Entra ID).** If `AZURE_OPENAI_API_KEY` is unset, the app signs in with `DefaultAzureCredential`
from `azure-identity`. The container image includes it; for a local install, add it with
`pip install -e ".[azure]"`. That credential tries, in order: environment variables, workload identity,
managed identity, and your `az login` session. The identity needs the **Cognitive Services OpenAI User** role on
the Azure OpenAI resource. With a user-assigned managed identity, also set `AZURE_CLIENT_ID`.

Your data stays in your Azure OpenAI resource's region and is governed by your Azure agreement. This is
usually the easiest route to approval for client data.

## OpenAI

```bash
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=<a vision-capable chat model>
```

## Limits and tokens

`AI_MAX_TOKENS` (default 4000) caps the answer length for every provider. With Claude, the older name
`CLAUDE_MAX_TOKENS` also works. Frames are downscaled on the phone to 1280 px on the long edge before upload,
which keeps image input small for every provider.

## Cost (rough guide)

A 4-frame segment is about 6,000 input tokens plus a few hundred output tokens. On Claude Haiku 5.5
($0.10 / $0.50 per million input / output tokens) that is roughly **$0.001 per segment**, about a cent per
minute of continuous scanning. Other providers and models differ: check their price lists. The phone waits for
each answer before recording the next segment, so requests never pile up.
