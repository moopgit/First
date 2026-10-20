"""Model providers.

`AnthropicProvider` calls Claude. `ScriptedProvider` replays a Python function
instead of a model, for tests; `DemoProvider` is a scripted team that exercises
the whole run (plan, parallel work, review, synthesis) offline, at zero cost.

Every provider returns assistant content as plain JSON-able dicts. The agent
loop appends them to the transcript unchanged (append-only history keeps
prompt caching and preserved thinking valid) and persists them to disk.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

# Models that accept server-side refusal fallbacks in the `"default"` form.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ProviderError(RuntimeError):
    """The model call failed for this task; the task escalates."""


class ProviderFatal(RuntimeError):
    """The model can't be called at all (credentials, permissions); the run pauses."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Turn:
    content: list[dict]
    stop_reason: str
    model: str
    usage: Usage = field(default_factory=Usage)
    refusal_category: str | None = None

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [ToolCall(b["id"], b["name"], b.get("input") or {}) for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()


class Provider(Protocol):
    name: str

    async def complete(
        self,
        *,
        model: str,
        effort: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict],
        meta: dict,
    ) -> Turn: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: Any = None, *, fallbacks: bool = True):
        self._client = client
        self.fallbacks = fallbacks

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            # Credentials resolve from ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile.
            try:
                self._client = anthropic.AsyncAnthropic(max_retries=4)
            except anthropic.AnthropicError as e:
                raise ProviderFatal(f"{e} - set ANTHROPIC_API_KEY or run `ant auth login`") from e
        return self._client

    async def complete(self, *, model, effort, max_tokens, system, messages, tools, meta) -> Turn:
        import anthropic

        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            # Stable prefix (tools + seat system prompt) gets an explicit breakpoint;
            # top-level cache_control caches the growing conversation tail.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            cache_control={"type": "ephemeral"},
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
        )
        betas = []
        if self.fallbacks and model in FALLBACK_MODELS:
            betas.append(FALLBACK_BETA)
            kwargs["fallbacks"] = "default"
        if betas:
            kwargs["betas"] = betas
        try:
            resp = await self.client.beta.messages.create(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise ProviderFatal(
                f"{type(e).__name__}: {getattr(e, 'message', e)} - set ANTHROPIC_API_KEY or run `ant auth login`"
            ) from e
        except anthropic.NotFoundError as e:
            raise ProviderFatal(f"model {model!r} not found: {getattr(e, 'message', e)}") from e
        except anthropic.RateLimitError as e:
            raise ProviderError(f"rate limited after retries: {getattr(e, 'message', e)}") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"API error {e.status_code}: {getattr(e, 'message', e)}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"network error after retries: {e}") from e

        u = resp.usage
        category = None
        if resp.stop_reason == "refusal" and getattr(resp, "stop_details", None):
            category = getattr(resp.stop_details, "category", None)
        return Turn(
            content=[b.model_dump(mode="json", exclude_none=True) for b in resp.content],
            stop_reason=resp.stop_reason or "end_turn",
            model=resp.model,
            usage=Usage(
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
            ),
            refusal_category=category,
        )


Script = Callable[[dict, list[dict]], "Turn | dict | Awaitable[Turn | dict]"]


class ScriptedProvider:
    """Calls `script(meta, messages)` instead of a model.

    The script returns a Turn, or a shorthand dict: {"tool": name, "input": {...}},
    {"tools": [(name, input), ...]}, or {"text": "..."}.
    """

    name = "scripted"

    def __init__(self, script: Script, *, delay: float = 0.0):
        self.script = script
        self.delay = delay
        self.calls: list[dict] = []
        self._n = 0

    async def complete(self, *, model, effort, max_tokens, system, messages, tools, meta) -> Turn:
        self.calls.append({"meta": dict(meta), "messages": json.loads(json.dumps(messages)), "tools": [t["name"] for t in tools]})
        if self.delay:
            await asyncio.sleep(self.delay)
        out = self.script(meta, messages)
        if asyncio.iscoroutine(out):
            out = await out
        return out if isinstance(out, Turn) else self._turn(out, model)

    def _turn(self, spec: dict, model: str) -> Turn:
        content: list[dict] = []
        if spec.get("text"):
            content.append({"type": "text", "text": spec["text"]})
        calls = list(spec.get("tools", []))
        if "tool" in spec:
            calls.append((spec["tool"], spec.get("input", {})))
        for name, args in calls:
            self._n += 1
            content.append({"type": "tool_use", "id": f"toolu_{self._n:04d}", "name": name, "input": args})
        stop = spec.get("stop_reason") or ("tool_use" if calls else "end_turn")
        usage = Usage(**spec["usage"]) if "usage" in spec else Usage(input_tokens=1000, output_tokens=200)
        return Turn(content=content, stop_reason=stop, model=model, usage=usage, refusal_category=spec.get("category"))


def turns_so_far(messages: list[dict]) -> int:
    return sum(1 for m in messages if m["role"] == "assistant")


def _demo_script(meta: dict, messages: list[dict]) -> dict:
    n = turns_so_far(messages)
    mode = meta["mode"]
    if mode == "plan":
        workers = meta["workers"][:3]
        tasks = []
        for i, seat in enumerate(workers, 1):
            tasks.append({
                "key": f"t{i}",
                "seat": seat,
                "goal": f"{seat}: contribute your lane's part of: {meta['goal']}",
                "acceptance": ["A deliverable file exists in your lane", "Claims that were not verified are labeled as such"],
                "depends_on": [] if i < 3 else ["t1", "t2"],
                "tier": "routine",
                "content_class": "general",
            })
        why = "Independent lanes run in parallel" + ("; the last one builds on both." if len(tasks) > 2 else ".")
        return {"tool": "submit_plan", "input": {"rationale": why, "tasks": tasks}}
    if mode == "work":
        writes = meta.get("writes") or []
        path = f"{writes[0].split('*')[0].rstrip('/') or 'notes'}/{meta['task'].lower()}.md" if writes else None
        if n == 0 and path:
            body = f"# {meta['task']} ({meta['seat']})\n\nDemo deliverable for: {meta['goal']}\n\n_Unverified: produced offline by the demo provider._\n"
            return {"text": "Writing the deliverable.", "tool": "write_file", "input": {"path": path, "content": body}}
        return {"tool": "submit", "input": {"summary": f"Wrote {path or 'no file'} for {meta['task']}.", "artifacts": [path] if path else []}}
    if mode == "review":
        if n == 0 and meta.get("artifacts"):
            return {"tool": "read_file", "input": {"path": meta["artifacts"][0]}}
        criteria = [{"criterion": c, "met": True, "evidence": "Checked the artifact."} for c in meta["acceptance"]]
        return {"tool": "submit_verdict", "input": {"verdict": "pass", "criteria": criteria, "feedback": "Meets the criteria."}}
    return {"tool": "submit", "input": {"summary": f"Synthesis of the team's work on: {meta['goal']}", "artifacts": []}}


class DemoProvider(ScriptedProvider):
    name = "demo"

    def __init__(self) -> None:
        super().__init__(_demo_script, delay=0.05)

    def _turn(self, spec: dict, model: str) -> Turn:
        return super()._turn({**spec, "usage": {}}, model)  # offline: zero tokens, zero cost
