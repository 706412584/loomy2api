"""Anthropic Messages API <-> OpenAI Chat Completions bridge.

Lets Claude Code (and anything else that speaks ``/v1/messages``) talk to the
Loomy upstream, including tool calls and streaming.  Reasoning output
(``reasoning_content``) is surfaced as Anthropic ``thinking`` blocks so nothing
is silently dropped.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["anthropic_to_openai", "openai_to_anthropic", "StreamTranslator",
           "estimate_tokens"]


def _blocks_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "tool_result":
                    parts.append(_blocks_to_text(block.get("content")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def _tool_result_text(block: Dict[str, Any]) -> str:
    content = block.get("content")
    text = content if isinstance(content, str) else _blocks_to_text(content)
    # an empty tool result makes some upstreams reject the whole request
    return text or "(empty)"


def _image_part(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Anthropic image block -> OpenAI ``image_url`` content part."""
    source = block.get("source") or {}
    if source.get("type") == "base64":
        media = source.get("media_type") or "image/png"
        url = f"data:{media};base64,{source.get('data') or ''}"
    elif source.get("type") == "url":
        url = source.get("url") or ""
    else:
        return None
    return {"type": "image_url", "image_url": {"url": url}} if url else None


def anthropic_to_openai(req: Dict[str, Any]) -> Dict[str, Any]:
    """Translate an Anthropic ``/v1/messages`` request into Chat Completions."""
    messages: List[Dict[str, Any]] = []

    system = req.get("system")
    if system:
        text = _blocks_to_text(system)
        # the billing header is Claude-Code private; upstream should not see it
        text = "\n".join(line for line in text.splitlines()
                         if "x-anthropic-billing-header" not in line).strip()
        if text:
            messages.append({"role": "system", "content": text})

    for message in req.get("messages") or []:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        texts: List[str] = []
        parts: List[Dict[str, Any]] = []          # ordered text/image parts
        images: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        thinking: List[str] = []
        tool_messages: List[Dict[str, Any]] = []

        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = str(block.get("text") or "")
                if text:
                    texts.append(text)
                    parts.append({"type": "text", "text": text})
            elif kind == "image":
                part = _image_part(block)
                if part:
                    images.append(part)
                    parts.append(part)
            elif kind == "tool_use":
                tool_calls.append({
                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {},
                                                ensure_ascii=False),
                    },
                })
            elif kind == "tool_result":
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or "",
                    "content": _tool_result_text(block),
                })
                # images inside a tool_result ride along on the user turn
                inner = block.get("content")
                if isinstance(inner, list):
                    for item in inner:
                        if isinstance(item, dict) and item.get("type") == "image":
                            part = _image_part(item)
                            if part:
                                images.append(part)
                                parts.append(part)
            elif kind == "thinking":
                chunk = str(block.get("thinking") or "")
                if chunk:
                    thinking.append(chunk)

        if role == "assistant":
            out_message: Dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(texts) or None,
            }
            if tool_calls:
                out_message["tool_calls"] = tool_calls
                # DeepSeek's thinking mode requires the reasoning that produced
                # a tool call to be replayed, or it rejects the request (400)
                if thinking:
                    out_message["reasoning_content"] = "\n".join(thinking)
            if out_message["content"] or tool_calls:
                messages.append(out_message)
            continue

        # tool_result blocks arrive inside a *user* message: they become
        # standalone "tool" messages, then the remaining text/images follow.
        messages.extend(tool_messages)
        if texts or images:
            body: Any = parts if images else "\n".join(texts)
            messages.append({"role": role, "content": body})
        elif not tool_messages:
            # an empty user/assistant turn still needs a slot; the upstream
            # accepts empty content but rejects an entirely empty list
            messages.append({"role": role, "content": ""})

    if not messages:
        messages.append({"role": "user", "content": ""})

    out: Dict[str, Any] = {
        "model": req.get("model"),
        "messages": messages,
        "stream": bool(req.get("stream")),
    }
    for src, dst in (("max_tokens", "max_tokens"), ("temperature", "temperature"),
                     ("top_p", "top_p")):
        if req.get(src) is not None:
            out[dst] = req[src]
    if req.get("stop_sequences"):
        out["stop"] = req["stop_sequences"]
    if isinstance(req.get("metadata"), dict):
        out["metadata"] = req["metadata"]
    out["parallel_tool_calls"] = True

    tools = req.get("tools") or []
    if tools:
        out["tools"] = [{
            "type": "function",
            "function": {
                "name": t.get("name"),
                "description": t.get("description") or "",
                "parameters": t.get("input_schema") or {"type": "object",
                                                        "properties": {}},
            },
        } for t in tools if t.get("name")]

    choice = req.get("tool_choice")
    if isinstance(choice, dict):
        if choice.get("type") == "auto":
            out["tool_choice"] = "auto"
        elif choice.get("type") == "any":
            out["tool_choice"] = "required"
        elif choice.get("type") == "none":
            out["tool_choice"] = "none"
        elif choice.get("type") == "tool" and choice.get("name"):
            out["tool_choice"] = {"type": "function",
                                 "function": {"name": choice["name"]}}
    return out


def openai_to_anthropic(obj: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Translate a non-streaming Chat Completion into an Anthropic message."""
    choice = (obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: List[Dict[str, Any]] = []

    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:                               # noqa: BLE001
            args = {}
        content.append({
            "type": "tool_use",
            "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
            "name": fn.get("name"),
            "input": args,
        })

    finish = choice.get("finish_reason")
    stop_reason = {"length": "max_tokens", "tool_calls": "tool_use"}.get(finish, "end_turn")
    usage = obj.get("usage") or {}
    return {
        "id": obj.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


def estimate_tokens(text: str) -> int:
    """Rough token estimate for the streaming ``message_start`` usage field."""
    return max(1, len(text) // 4)


class StreamTranslator:
    """Converts OpenAI SSE chunks into Anthropic SSE events.

    Usage::

        st = StreamTranslator(model)
        for event, data in st.start(): ...      # emit
        for chunk in openai_chunks:
            for event, data in st.feed(chunk): ...
        for event, data in st.finish(): ...     # emit
    """

    def __init__(self, model: str):
        self.model = model
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.usage: Dict[str, Any] = {}
        self.finish_reason: Optional[str] = None
        # every content block (thinking / text / each tool_use) needs its own
        # index; allocate monotonically instead of pinning them all to 0
        self._next_index = 0
        self._think_index: Optional[int] = None
        self._think_sig = ""
        self._text_index: Optional[int] = None
        # upstream streams tool_calls in fragments: OpenAI tool index -> block
        self._tools: Dict[int, Dict[str, Any]] = {}
        self._open_tool: Optional[int] = None

    # -- helpers --------------------------------------------------------

    def _ev(self, event: str, data: Dict[str, Any]):
        return event, data

    def start(self) -> Iterable:
        yield self._ev("message_start", {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

    def _close_think(self) -> Iterable:
        if self._think_index is not None:
            # Claude Code replays thinking blocks verbatim on the next turn;
            # a signature_delta lets it carry this block back (empty is fine
            # when the upstream signs nothing).
            yield self._ev("content_block_delta", {
                "type": "content_block_delta", "index": self._think_index,
                "delta": {"type": "signature_delta", "signature": self._think_sig}})
            yield self._ev("content_block_stop", {
                "type": "content_block_stop", "index": self._think_index})
            self._think_index = None
            self._think_sig = ""

    def _close_text(self) -> Iterable:
        if self._text_index is not None:
            yield self._ev("content_block_stop", {
                "type": "content_block_stop", "index": self._text_index})
            self._text_index = None

    def feed(self, obj: Dict[str, Any]) -> Iterable:
        usage = obj.get("usage")
        if isinstance(usage, dict):
            self.usage.update(usage)

        choices = obj.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}

        thinking = delta.get("reasoning_content") or delta.get("reasoning")
        if thinking:
            # a tool call or text ends the thinking block
            if self._think_index is None:
                yield from self._close_text()
                self._think_index = self._next_index
                self._next_index += 1
                yield self._ev("content_block_start", {
                    "type": "content_block_start", "index": self._think_index,
                    "content_block": {"type": "thinking", "thinking": ""}})
            yield self._ev("content_block_delta", {
                "type": "content_block_delta", "index": self._think_index,
                "delta": {"type": "thinking_delta", "thinking": thinking}})

        text = delta.get("content")
        if text:
            if self._think_index is not None:
                yield from self._close_think()
            if self._text_index is None:
                self._text_index = self._next_index
                self._next_index += 1
                yield self._ev("content_block_start", {
                    "type": "content_block_start", "index": self._text_index,
                    "content_block": {"type": "text", "text": ""}})
            yield self._ev("content_block_delta", {
                "type": "content_block_delta", "index": self._text_index,
                "delta": {"type": "text_delta", "text": text}})

        # upstream streams tool calls in fragments: the first fragment carries
        # id/name and an empty arguments string, later fragments carry argument
        # text only.  Accumulate per OpenAI tool index and forward the raw JSON
        # text, so the client can reassemble full arguments.
        for call in delta.get("tool_calls") or []:
            fn = call.get("function") or {}
            tidx = call.get("index")
            if tidx is None:
                tidx = len(self._tools)
            block = self._tools.get(tidx)
            if block is None:
                # a new tool call begins; close whatever block is still open
                if self._think_index is not None:
                    yield from self._close_think()
                if self._text_index is not None:
                    yield from self._close_text()
                block = {
                    "index": self._next_index,
                    "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "name": fn.get("name") or "",
                    "args_text": "",
                }
                self._next_index += 1
                self._tools[tidx] = block
                yield self._ev("content_block_start", {
                    "type": "content_block_start", "index": block["index"],
                    "content_block": {"type": "tool_use", "id": block["id"],
                                      "name": block["name"], "input": {}}})
            # a later fragment may supply the id/name the first one omitted
            if not block["id"] and call.get("id"):
                block["id"] = call["id"]
            if not block["name"] and fn.get("name"):
                block["name"] = fn["name"]
            args = fn.get("arguments")
            if args:
                block["args_seen"] = True
                block["args_text"] += args
                yield self._ev("content_block_delta", {
                    "type": "content_block_delta", "index": block["index"],
                    "delta": {"type": "input_json_delta",
                              "partial_json": args}})

        self.finish_reason = choice.get("finish_reason") or self.finish_reason

    def truncated_tool(self) -> bool:
        """True when a tool call's accumulated arguments are not valid JSON.

        An upstream stream that dies mid tool-call leaves a half-written
        ``partial_json`` — delivering it as a normal ``tool_use`` block makes
        the client attempt to parse garbage (seen as "could not be parsed as
        JSON" errors).  The stream handler calls this on abnormal termination
        and reports an error instead of pretending the message completed.
        """
        for block in self._tools.values():
            text = block.get("args_text") or ""
            if text:
                try:
                    json.loads(text)
                except Exception:                       # noqa: BLE001
                    return True
            # a clean no-argument tool (id+name present, no args fragments):
            # finish() patches in "{}" as usual, nothing broken
        return False

    def finish(self) -> Iterable:
        yield from self._close_think()
        yield from self._close_text()
        for block in self._tools.values():
            # some clients only build tool input from deltas, so a tool with no
            # arguments at all still needs an explicit empty object
            if not block.get("args_seen"):
                yield self._ev("content_block_delta", {
                    "type": "content_block_delta", "index": block["index"],
                    "delta": {"type": "input_json_delta", "partial_json": "{}"}})
            yield self._ev("content_block_stop", {
                "type": "content_block_stop", "index": block["index"]})
        has_tool = bool(self._tools)
        self._tools.clear()
        stop_reason = {"length": "max_tokens", "tool_calls": "tool_use"}.get(
            self.finish_reason, "tool_use" if has_tool else "end_turn")
        yield self._ev("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": self.usage.get("completion_tokens") or 0},
        })
        yield self._ev("message_stop", {"type": "message_stop"})
