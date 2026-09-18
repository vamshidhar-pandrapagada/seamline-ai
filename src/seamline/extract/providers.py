"""LLM providers behind one small interface: send a system prompt, an excerpt and a JSON
schema; get back schema-shaped data and token counts.

The Anthropic API (with the user's own API key) is the only real provider. A `claude -p`
provider on a Claude subscription was used to validate extraction during Phase 2 and then
removed: third-party tools may not offer claude.ai login (Claude Agent SDK docs).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import anthropic

from seamline.config import Config


class ProviderError(Exception):
    """The model call failed or returned something unusable. Retrying later may help."""


class ProviderAuthError(ProviderError):
    """No usable credentials. Every call would fail the same way, so stop."""


@dataclass
class Completion:
    data: dict
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


@dataclass(frozen=True)
class Tool:
    """The form the model fills in: its name, what it's for, and its JSON schema."""

    name: str
    description: str


RECORD_FACTS = Tool("record_facts", "Record the facts extracted from the session excerpt.")


class LLMProvider(Protocol):
    name: str

    def complete(
        self, system: str, prompt: str, schema: dict, tool: Tool = RECORD_FACTS
    ) -> Completion: ...


NO_CREDENTIALS = (
    "no Anthropic API credentials found: create a key at https://console.anthropic.com and "
    "set ANTHROPIC_API_KEY in your shell environment"
)


class AnthropicProvider:
    """The Messages API. Credentials come from the environment (ANTHROPIC_API_KEY or an
    `ant auth login` profile); Seamline never stores them.

    Two ways to get schema-shaped output:
    - "tool" (default): the model records facts by calling a strict tool whose input schema
      is the fact schema. This is how the Claude Code CLI returned facts in Phase 2 testing.
    - "schema": structured outputs (`output_config.format`). In the first live run this
      returned an empty list on an excerpt the tool route extracted 17 facts from.
    """

    name = "anthropic"
    MAX_TOKENS = 16000
    MODES = ("tool", "schema")
    # Models with safety classifiers that can decline a request: opt into Anthropic's
    # server-side fallback, which re-runs a declined request on a suitable other model.
    FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(
        self,
        model: str,
        client: anthropic.Anthropic | None = None,
        mode: str = "tool",
        debug: bool = False,
    ):
        if mode not in self.MODES:
            raise ProviderError(f"unknown structured-output mode {mode!r}")
        self.model = model
        self.mode = mode
        self.debug = debug
        self.client = client or anthropic.Anthropic(max_retries=3)

    def _request(self, system: str, prompt: str, schema: dict, tool: Tool) -> dict:
        request = {
            "model": self.model,
            "max_tokens": self.MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.mode == "schema":
            request["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        else:
            request["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": schema,
                    "strict": True,
                }
            ]
            request["tool_choice"] = {"type": "tool", "name": tool.name}
        return request

    def complete(
        self, system: str, prompt: str, schema: dict, tool: Tool = RECORD_FACTS
    ) -> Completion:
        try:
            request = self._request(system, prompt, schema, tool)
            if self.model in self.FALLBACK_MODELS:
                response = self.client.beta.messages.create(
                    **request, betas=[self.FALLBACK_BETA], fallbacks="default"
                )
            else:
                response = self.client.messages.create(**request)
        except anthropic.CredentialsError as e:
            raise ProviderAuthError(NO_CREDENTIALS) from e
        except TypeError as e:  # The SDK's "Could not resolve authentication method"
            if "authentication method" not in str(e):
                raise
            raise ProviderAuthError(NO_CREDENTIALS) from e
        except anthropic.AuthenticationError as e:
            raise ProviderAuthError(
                "Anthropic API rejected the credentials: check ANTHROPIC_API_KEY"
            ) from e
        except anthropic.BadRequestError as e:
            raise ProviderError(f"request rejected: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise ProviderError("rate limited by the Anthropic API; try again later") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("could not reach the Anthropic API") from e

        if self.debug:
            _print_debug(response)
        if response.stop_reason == "refusal":
            raise ProviderError("the model declined this excerpt")
        if response.stop_reason == "max_tokens":
            raise ProviderError("output hit max_tokens; the excerpt produced too many facts")
        if self.mode == "tool":
            call = next(
                (b for b in response.content if b.type == "tool_use" and b.name == tool.name),
                None,
            )
            if call is None:
                raise ProviderError(f"the model did not call the {tool.name} tool")
            data = call.input
        else:
            text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                data = json.loads(text)
            except ValueError as e:
                raise ProviderError("model returned invalid JSON") from e
        return Completion(
            data=data,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
        )


def _print_debug(response) -> None:
    """Show what the model actually returned (to stderr, so it never mixes with results)."""
    import sys

    print(
        f"[debug] stop_reason={response.stop_reason} "
        f"input_tokens={response.usage.input_tokens} output_tokens={response.usage.output_tokens}",
        file=sys.stderr,
    )
    for block in response.content:
        if block.type == "text":
            body = block.text
        elif block.type == "tool_use":
            body = json.dumps(block.input)
        else:
            body = ""
        print(f"[debug] {block.type}: {body[:1500]}", file=sys.stderr)


class FakeProvider:
    """Canned responses for tests: a list consumed in order, or a function of the prompt."""

    name = "fake"

    def __init__(self, responses: list[dict] | Callable[[str], dict]):
        self.responses = responses
        self.prompts: list[str] = []

    def complete(
        self, system: str, prompt: str, schema: dict, tool: Tool = RECORD_FACTS
    ) -> Completion:
        self.prompts.append(prompt)
        if callable(self.responses):
            data = self.responses(prompt)
        elif self.responses:
            data = self.responses.pop(0)
        else:
            data = {"facts": []}
        if isinstance(data, Exception):
            raise data
        return Completion(data=data, input_tokens=len(prompt) // 4, output_tokens=10, model="fake")


def make_provider(
    config: Config,
    provider: str | None = None,
    model: str | None = None,
    *,
    structured: str = "tool",
    debug: bool = False,
):
    name = provider or config.extract.provider
    model = model or config.extract.model
    if name == "anthropic":
        return AnthropicProvider(model, mode=structured, debug=debug)
    raise ProviderError(f"unknown provider {name!r}")
