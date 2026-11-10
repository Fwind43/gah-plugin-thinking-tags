"""thinking-tags — ga-harness plugin for GenericAgent PR #861.

Brings GenericAgent PR #861 (`feat(llmcore): wrap native reasoning in <thinking>
tags and extract inline thinking`) into ga-harness as a hot-loadable plugin
without modifying GenericAgent source files:

1. Wraps native reasoning blocks and SSE deltas in ``<thinking>...</thinking>``
   across Claude (``thinking`` blocks / ``thinking_delta``) and OpenAI
   (Chat Completions ``reasoning_content`` / ``reasoning``, plus Responses API
   ``reasoning`` items & deltas).
2. Extracts inline ``<think>...</think>`` and ``<thinking>...</thinking>`` tags
   from ``response.content`` into ``response.thinking`` and strips them from
   ``response.content`` before text tool-call fallback parsing.
3. Strips ``<think>`` / ``<thinking>`` blocks from ``llmcore.fast_ask()`` output.
4. Automatically detects if the underlying ``llmcore`` already has PR #861
   applied and acts as a zero-duplication passthrough.
"""
from __future__ import annotations

import json
import re
import sys
from collections import deque
from typing import Any, Callable

_THINK_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL)
_SENTINEL = "__GAH_THINKING_CLOSE_9f3c7a__"
_CLAUDE_CLOSE_SSE = (
    'data: {"type":"content_block_delta","delta":'
    f'{{"type":"thinking_delta","thinking":"{_SENTINEL}"}}}}'
)
_OPENAI_PULSE_SSE = (
    f'data: {{"type":"response.output_text.delta","delta":"{_SENTINEL}"}}'
)


def _setting(ctx: Any, key: str, default: bool = True) -> bool:
    settings = getattr(ctx, "settings", None)
    if isinstance(settings, dict) and key in settings:
        return bool(settings[key])
    return default


def _unwrap(fn: Any) -> Any:
    cur = fn
    seen = set()
    while getattr(cur, "__wrapped__", None) is not None and id(cur) not in seen:
        seen.add(id(cur))
        cur = cur.__wrapped__
    return cur


def _has_native_thinking_tags(fn: Any) -> bool:
    """Return True if ``fn`` (or its unwrapped target) already emits ``<thinking>``."""
    cur = _unwrap(fn)
    code = getattr(cur, "__code__", None)
    consts = getattr(code, "co_consts", ()) if code else ()
    return any(
        isinstance(c, str) and ("<thinking>" in c or "</thinking>" in c)
        for c in consts
    )


def _decode_sse_json(raw_line: Any) -> dict | None:
    line = (
        raw_line.decode("utf-8", errors="replace")
        if isinstance(raw_line, bytes)
        else raw_line
    )
    if not isinstance(line, str) or not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        evt = json.loads(payload)
    except Exception:
        return None
    return evt if isinstance(evt, dict) else None


def _make_parse_claude_json(orig: Callable, enabled_fn: Callable[[], bool]) -> Callable:
    native = _has_native_thinking_tags(orig)

    def _parse_claude_json(data):
        if (
            not enabled_fn()
            or native
            or not isinstance(data, dict)
            or data.get("type") == "error"
        ):
            return (yield from orig(data))
        yielded_blocks = [
            b
            for b in (data.get("content") or [])
            if isinstance(b, dict) and b.get("type") in ("text", "thinking")
        ]
        gen = orig(data)
        idx = 0
        try:
            while True:
                chunk = next(gen)
                blk = yielded_blocks[idx] if idx < len(yielded_blocks) else {}
                idx += 1
                if (
                    blk.get("type") == "thinking"
                    and isinstance(chunk, str)
                    and not chunk.startswith("<thinking>")
                ):
                    t = chunk or blk.get("thinking") or ""
                    yield f"<thinking>{t}</thinking>" if t else ""
                else:
                    yield chunk
        except StopIteration as e:
            return e.value

    _parse_claude_json.__wrapped__ = orig
    _parse_claude_json._gah_thinking_tags_wrapped = True
    return _parse_claude_json


def _make_parse_openai_json(orig: Callable, enabled_fn: Callable[[], bool]) -> Callable:
    native = _has_native_thinking_tags(orig)

    def _parse_openai_json(data, api_mode="chat_completions"):
        if not enabled_fn() or native or not isinstance(data, dict):
            return (yield from orig(data, api_mode))
        events: list[tuple[str, str]] = []
        thinking_texts: list[str] = []
        if api_mode == "responses":
            for item in data.get("output") or []:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type")
                if itype == "message":
                    for p in item.get("content") or []:
                        if (
                            isinstance(p, dict)
                            and p.get("type") in ("output_text", "text")
                            and p.get("text")
                        ):
                            events.append(("text", p["text"]))
                elif itype == "reasoning":
                    for p in (item.get("content") or []) + (item.get("summary") or []):
                        if (
                            isinstance(p, dict)
                            and p.get("type") in ("reasoning_text", "summary_text")
                            and p.get("text")
                        ):
                            events.append(("thinking", p["text"]))
                            thinking_texts.append(p["text"])
        else:
            choices = data.get("choices") or [{}]
            first = choices[0] if choices and isinstance(choices[0], dict) else {}
            msg = first.get("message") or {}
            if isinstance(msg, dict):
                reasoning = msg.get("reasoning_content") or msg.get("reasoning", "")
                if reasoning:
                    events.append(("thinking", reasoning))
                    thinking_texts.append(reasoning)
                if msg.get("content", ""):
                    events.append(("text", msg["content"]))

        gen = orig(data, api_mode)
        ev_idx = 0
        while ev_idx < len(events) and events[ev_idx][0] == "thinking":
            yield f"<thinking>{events[ev_idx][1]}</thinking>"
            ev_idx += 1
        try:
            while True:
                chunk = next(gen)
                if ev_idx < len(events) and events[ev_idx][0] == "text":
                    ev_idx += 1
                yield chunk
                while ev_idx < len(events) and events[ev_idx][0] == "thinking":
                    yield f"<thinking>{events[ev_idx][1]}</thinking>"
                    ev_idx += 1
        except StopIteration as e:
            while ev_idx < len(events):
                if events[ev_idx][0] == "thinking":
                    yield f"<thinking>{events[ev_idx][1]}</thinking>"
                ev_idx += 1
            blocks = e.value
            if isinstance(blocks, list) and thinking_texts:
                has_thinking = any(
                    isinstance(b, dict) and b.get("type") == "thinking" for b in blocks
                )
                if not has_thinking:
                    blocks = [
                        {"type": "thinking", "thinking": t} for t in thinking_texts
                    ] + blocks
            return blocks

    _parse_openai_json.__wrapped__ = orig
    _parse_openai_json._gah_thinking_tags_wrapped = True
    return _parse_openai_json


def _make_parse_claude_sse(orig: Callable, enabled_fn: Callable[[], bool]) -> Callable:
    native = _has_native_thinking_tags(orig)

    def _parse_claude_sse(resp_lines):
        if not enabled_fn() or native:
            return (yield from orig(resp_lines))

        pending: deque[str] = deque()
        state = {"in_thinking": False, "passthrough": False, "block_type": None}

        def _tracked_lines():
            for raw_line in resp_lines:
                evt = _decode_sse_json(raw_line)
                evt_type = evt.get("type") if evt else None
                was_thinking_block = False
                if evt_type == "content_block_start":
                    cb = evt.get("content_block") or {}
                    state["block_type"] = cb.get("type") if isinstance(cb, dict) else None
                elif evt_type == "content_block_delta":
                    delta = evt.get("delta") or {}
                    if isinstance(delta, dict):
                        dtype = delta.get("type")
                        if dtype == "thinking_delta" and delta.get("thinking", ""):
                            pending.append("thinking")
                        elif dtype == "text_delta" and delta.get("text", ""):
                            pending.append("text")
                elif evt_type == "content_block_stop":
                    was_thinking_block = (
                        state["block_type"] == "thinking" or state["in_thinking"]
                    )
                    state["block_type"] = None
                    if (
                        was_thinking_block
                        and state["in_thinking"]
                        and not state["passthrough"]
                    ):
                        pending.append("close_thinking")
                        yield _CLAUDE_CLOSE_SSE

                yield raw_line

        gen = orig(_tracked_lines())
        try:
            while True:
                chunk = next(gen)
                if state["passthrough"] or not isinstance(chunk, str):
                    yield chunk
                    continue
                kind = pending.popleft() if pending else "text"
                if kind == "close_thinking" or chunk == _SENTINEL:
                    if state["in_thinking"]:
                        state["in_thinking"] = False
                        yield "</thinking>"
                elif kind == "thinking":
                    if chunk.startswith("<thinking>") or chunk.startswith("</thinking>"):
                        state["passthrough"] = True
                        yield chunk
                    elif not state["in_thinking"]:
                        state["in_thinking"] = True
                        yield f"<thinking>{chunk}"
                    else:
                        yield chunk
                else:
                    if chunk.startswith("<thinking>") or chunk.startswith("</thinking>"):
                        state["passthrough"] = True
                        yield chunk
                    elif state["in_thinking"]:
                        state["in_thinking"] = False
                        yield f"</thinking>{chunk}"
                    else:
                        yield chunk
        except StopIteration as e:
            if state["in_thinking"] and not state["passthrough"]:
                state["in_thinking"] = False
                yield "</thinking>"
            blocks = e.value
            if isinstance(blocks, list):
                cleaned_blocks = []
                for b in blocks:
                    if (
                        isinstance(b, dict)
                        and b.get("type") == "thinking"
                        and _SENTINEL in (b.get("thinking") or "")
                    ):
                        b = dict(b)
                        b["thinking"] = b["thinking"].replace(_SENTINEL, "")
                        if not b["thinking"] and not b.get("signature"):
                            continue
                    cleaned_blocks.append(b)
                blocks = cleaned_blocks
            return blocks

    _parse_claude_sse.__wrapped__ = orig
    _parse_claude_sse._gah_thinking_tags_wrapped = True
    return _parse_claude_sse


def _make_parse_openai_sse(orig: Callable, enabled_fn: Callable[[], bool]) -> Callable:
    native = _has_native_thinking_tags(orig)

    def _parse_openai_sse(resp_lines, api_mode="chat_completions"):
        if not enabled_fn() or native:
            return (yield from orig(resp_lines, api_mode))

        pending: deque[tuple[str, str | None]] = deque()
        state = {
            "in_thinking": False,
            "passthrough": False,
            "reasoning_text": "",
        }

        def _tracked_lines():
            seen_delta = False
            seen_reasoning_delta = False
            for raw_line in resp_lines:
                evt = _decode_sse_json(raw_line)
                if evt is None:
                    yield raw_line
                    continue

                if api_mode == "responses":
                    etype = evt.get("type", "")
                    if etype == "response.output_text.delta":
                        if evt.get("delta", ""):
                            seen_delta = True
                            pending.append(("text", None))
                        yield raw_line
                    elif etype == "response.output_text.done":
                        text = evt.get("text", "")
                        if not seen_delta and text:
                            pending.append(("text", None))
                            yield (
                                "data: "
                                + json.dumps(
                                    {
                                        "type": "response.output_text.delta",
                                        "delta": text,
                                    }
                                )
                            )
                        else:
                            yield raw_line
                    elif etype in (
                        "response.reasoning_text.delta",
                        "response.reasoning_summary_text.delta",
                    ):
                        delta = evt.get("delta", "")
                        yield raw_line
                        if delta:
                            seen_reasoning_delta = True
                            state["reasoning_text"] += delta
                            pending.append(("thinking", delta))
                            yield _OPENAI_PULSE_SSE
                    elif etype in (
                        "response.reasoning_text.done",
                        "response.reasoning_summary_text.done",
                    ):
                        text = evt.get("text", "")
                        yield raw_line
                        if text and not seen_reasoning_delta:
                            sep = "\n" if state["reasoning_text"] else ""
                            r_chunk = f"{sep}{text}"
                            state["reasoning_text"] += r_chunk
                            pending.append(("thinking", r_chunk))
                            yield _OPENAI_PULSE_SSE
                        seen_reasoning_delta = False
                    elif etype == "response.output_item.done":
                        item = evt.get("item") or {}
                        yield raw_line
                        if isinstance(item, dict) and item.get("type") == "reasoning":
                            if not state["reasoning_text"]:
                                parts = [
                                    p["text"]
                                    for p in (item.get("content") or [])
                                    + (item.get("summary") or [])
                                    if isinstance(p, dict)
                                    and p.get("type")
                                    in ("reasoning_text", "summary_text")
                                    and p.get("text")
                                ]
                                if parts:
                                    r_chunk = "\n".join(parts)
                                    state["reasoning_text"] = r_chunk
                                    pending.append(("thinking", r_chunk))
                                    yield _OPENAI_PULSE_SSE
                            if (
                                state["in_thinking"]
                                or any(k == "thinking" for k, _ in pending)
                            ) and not state["passthrough"]:
                                pending.append(("close_thinking", None))
                                yield _OPENAI_PULSE_SSE
                    else:
                        yield raw_line
                else:
                    choices = evt.get("choices") or [{}]
                    ch = (
                        choices[0]
                        if choices and isinstance(choices[0], dict)
                        else {}
                    )
                    delta = ch.get("delta") or {}
                    if isinstance(delta, dict):
                        if delta.get("reasoning_content") or delta.get(
                            "reasoning", ""
                        ):
                            pending.append(("thinking", None))
                        if delta.get("content"):
                            pending.append(("text", None))
                    yield raw_line

        gen = orig(_tracked_lines(), api_mode)
        try:
            while True:
                chunk = next(gen)
                if state["passthrough"] or not isinstance(chunk, str):
                    yield chunk
                    continue
                kind, override_val = (
                    pending.popleft() if pending else ("text", None)
                )
                text_val = override_val if override_val is not None else chunk
                if kind == "close_thinking" or (
                    kind != "thinking" and chunk == _SENTINEL
                ):
                    if state["in_thinking"]:
                        state["in_thinking"] = False
                        yield "</thinking>"
                elif kind == "thinking":
                    if text_val.startswith("<thinking>") or text_val.startswith(
                        "</thinking>"
                    ):
                        state["passthrough"] = True
                        yield text_val
                    elif not state["in_thinking"]:
                        state["in_thinking"] = True
                        yield f"<thinking>{text_val}"
                    else:
                        yield text_val
                else:
                    if text_val.startswith("<thinking>") or text_val.startswith(
                        "</thinking>"
                    ):
                        state["passthrough"] = True
                        yield text_val
                    elif state["in_thinking"]:
                        state["in_thinking"] = False
                        yield f"</thinking>{text_val}"
                    else:
                        yield text_val
        except StopIteration as e:
            if state["in_thinking"] and not state["passthrough"]:
                state["in_thinking"] = False
                yield "</thinking>"
            blocks = e.value
            if isinstance(blocks, list) and api_mode == "responses":
                cleaned_blocks = []
                has_thinking = False
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "thinking":
                        has_thinking = True
                        if state["reasoning_text"]:
                            b["thinking"] = state["reasoning_text"]
                        cleaned_blocks.append(b)
                    elif isinstance(b, dict) and b.get("type") == "text":
                        txt = b.get("text", "")
                        if isinstance(txt, str) and _SENTINEL in txt:
                            txt = txt.replace(_SENTINEL, "")
                            b["text"] = txt
                        if b.get("text"):
                            cleaned_blocks.append(b)
                    else:
                        cleaned_blocks.append(b)
                if state["reasoning_text"] and not has_thinking:
                    cleaned_blocks.insert(
                        0,
                        {"type": "thinking", "thinking": state["reasoning_text"]},
                    )
                blocks = cleaned_blocks
            return blocks

    _parse_openai_sse.__wrapped__ = orig
    _parse_openai_sse._gah_thinking_tags_wrapped = True
    return _parse_openai_sse


def _extract_inline_thinking_blocks(blocks: Any) -> Any:
    if not isinstance(blocks, list):
        return blocks
    new_blocks: list[Any] = []
    for b in blocks:
        if (
            isinstance(b, dict)
            and b.get("type") == "text"
            and isinstance(b.get("text"), str)
            and _THINK_RE.search(b["text"])
        ):
            raw_text = b["text"]
            extracted = [
                m.strip() for m in _THINK_RE.findall(raw_text) if m and m.strip()
            ]
            cleaned = _THINK_RE.sub("", raw_text).strip()
            if extracted:
                new_blocks.append(
                    {"type": "thinking", "thinking": "\n".join(extracted)}
                )
            b_copy = dict(b)
            b_copy["text"] = cleaned
            new_blocks.append(b_copy)
        else:
            new_blocks.append(b)
    return new_blocks


def _extract_inline_thinking_from_response(resp: Any, mod: Any = None) -> Any:
    if resp is None:
        return resp
    content = getattr(resp, "content", None)
    if not isinstance(content, str) or not _THINK_RE.search(content):
        return resp
    extracted = [m.strip() for m in _THINK_RE.findall(content) if m and m.strip()]
    cleaned = _THINK_RE.sub("", content).strip()
    existing = (getattr(resp, "thinking", None) or "").strip()
    parts = ([existing] if existing else []) + extracted
    resp.thinking = "\n".join(parts).strip()

    parse_tc = getattr(mod, "_parse_text_tool_calls", None) if mod is not None else None
    raw = getattr(resp, "raw", None)
    has_native_tool_use = isinstance(raw, str) and "'type': 'tool_use'" in raw
    if callable(parse_tc) and not has_native_tool_use:
        tc, cleaned = parse_tc(cleaned)
        resp.tool_calls = tc
    resp.content = cleaned
    return resp


def _make_native_ask(
    orig: Callable, mod: Any, enabled_fn: Callable[[], bool]
) -> Callable:
    def ask(self, *args, **kwargs):
        if not enabled_fn():
            return (yield from orig(self, *args, **kwargs))
        orig_raw_ask = getattr(self, "raw_ask", None)
        has_own_raw_ask = "raw_ask" in getattr(self, "__dict__", {})
        if callable(orig_raw_ask):
            def _wrapped_raw_ask(*r_args, **r_kwargs):
                blocks = yield from orig_raw_ask(*r_args, **r_kwargs)
                return _extract_inline_thinking_blocks(blocks)

            self.raw_ask = _wrapped_raw_ask
        try:
            resp = yield from orig(self, *args, **kwargs)
        finally:
            if callable(orig_raw_ask):
                if has_own_raw_ask:
                    self.raw_ask = orig_raw_ask
                else:
                    self.__dict__.pop("raw_ask", None)
        return _extract_inline_thinking_from_response(resp, mod)

    ask.__wrapped__ = orig
    ask._gah_thinking_tags_wrapped = True
    return ask


def _make_fast_ask(orig: Callable, enabled_fn: Callable[[], bool]) -> Callable:
    def fast_ask(prompt, cfg_name):
        out = orig(prompt, cfg_name)
        if not enabled_fn() or not isinstance(out, str):
            return out
        return _THINK_RE.sub("", out).strip()

    fast_ask.__wrapped__ = orig
    fast_ask._gah_thinking_tags_wrapped = True
    return fast_ask


def _resolve_llmcore(agent: Any) -> Any:
    mod = sys.modules.get("llmcore")
    if mod is not None:
        return mod
    client = getattr(agent, "llm_client", None)
    if client is not None:
        return sys.modules.get(type(client).__module__)
    return None


def apply(ctx) -> None:
    agent = ctx.get("ga.agent") if hasattr(ctx, "get") else None
    hooks = ctx.get("ga.hooks") if hasattr(ctx, "get") else None
    mod = _resolve_llmcore(agent)

    wrap_enabled = lambda: _setting(ctx, "wrap_native_reasoning", True)
    extract_enabled = lambda: _setting(ctx, "extract_inline_thinking", True)
    strip_fast_enabled = lambda: _setting(ctx, "strip_fast_ask_thinking", True)

    saved_attrs: list[tuple[Any, str, Any]] = []

    def _patch(target: Any, attr: str, factory: Callable[[Any], Any]) -> None:
        orig = getattr(target, attr, None)
        if not callable(orig) or getattr(orig, "_gah_thinking_tags_wrapped", False):
            return
        wrapped = factory(orig)
        setattr(target, attr, wrapped)
        saved_attrs.append((target, attr, orig))

    if mod is not None:
        _patch(mod, "_parse_claude_json", lambda f: _make_parse_claude_json(f, wrap_enabled))
        _patch(mod, "_parse_claude_sse", lambda f: _make_parse_claude_sse(f, wrap_enabled))
        _patch(mod, "_parse_openai_json", lambda f: _make_parse_openai_json(f, wrap_enabled))
        _patch(mod, "_parse_openai_sse", lambda f: _make_parse_openai_sse(f, wrap_enabled))
        _patch(mod, "fast_ask", lambda f: _make_fast_ask(f, strip_fast_enabled))

        for cls_name in ("NativeClaudeSession", "OpenAISession", "LLMSession"):
            sess_cls = getattr(mod, cls_name, None)
            if sess_cls is not None:
                _patch(
                    sess_cls,
                    "ask",
                    lambda f: _make_native_ask(f, mod, extract_enabled),
                )

    if hooks is not None and hasattr(hooks, "on"):
        def _on_llm_after(*args):
            if not extract_enabled() or not args:
                return
            resp = args[-1]
            _extract_inline_thinking_from_response(resp, mod)

        hooks.on("llm_after", _on_llm_after)

    if hasattr(ctx, "scope") and saved_attrs:
        def _restore():
            for target, attr, orig in reversed(saved_attrs):
                try:
                    setattr(target, attr, orig)
                except Exception:
                    pass

        if callable(ctx.scope):
            ctx.scope(_restore)
        elif hasattr(ctx.scope, "add"):
            ctx.scope.add(_restore)
