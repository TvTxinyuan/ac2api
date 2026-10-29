"""把 OpenAI Responses 协议翻译成 Chat Completions 协议。

为什么需要这一层
---------------
Codex CLI / ChatGPT 桌面端的配置里写的是 `wire_api = "responses"`，也就是
它们只会请求 `POST /v1/responses`，请求体和响应体都是 Responses 的形状
（`input` / `output` / SSE 事件 `response.output_text.delta` …）。而 AutoClaw
上游说的是最标准的 Chat Completions（`messages` / `choices`）。

所以这里做两件事，**只做翻译，不做业务**：
    responses → chat   请求体降级（to_chat_request）
    chat → responses   非流式响应升级（to_response_object）
    chat SSE → responses SSE      流式事件升级（StreamTranslator）

刻意的取舍（写给以后改这块的人）：
* **不假装 chat 里没有的能力**。Responses 的 `previous_response_id`、
  `reasoning.encrypted_content` 在上游没有对应物，直接丢弃并保持状态自洽
  —— Codex 靠的是「每轮把完整 input 重发」，不依赖服务端存对话。
* **函数调用桥接**。Responses 用顶层的 `tools:[{type:"function", name, ...}]`
  + `output` 里的 `function_call` 项；chat 用 `tools:[{type:"function",
  function:{...}}]` + `tool_calls`。两边语义一致，只是形状不同。
* **`store` 恒为 false**。网关不存对话，也不接受上游存。
* **事件序号/时间戳每次响应独立**。Codex 按 `sequence_number` 递增消费，
  跳号或重复会让它卡住，所以计数器只增不减。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable

#: 需要从 Responses 请求里丢弃的字段：要么是网关不支持的宿主侧能力，
#: 要么是会在 chat 协议里引发 400 的「Responses 专属」字段。
DROP_REQUEST_FIELDS = (
    "background", "store", "stream_options", "include", "previous_response_id",
    "prompt", "prompt_cache_key", "reasoning", "safety_identifier", "service_tier",
    "text", "truncation", "max_tool_calls", "metadata", "top_logprobs", "user",
)


def new_id(prefix: str) -> str:
    """生成 `resp_...` / `msg_...` 这类带前缀的 id。"""
    return f"{prefix}_{uuid.uuid4().hex}"


def text_of(content: Any) -> str:
    """把 Responses 的 content（字符串或部件数组）压成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                # input_text / output_text / text 都是同一个含义
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "".join(parts)
    return ""


#: 向后兼容的别名（包内早期代码用的是私有名）。
_text_of = text_of


def _image_blocks(content: Any) -> list[dict]:
    """把 input_image 部件转成 chat 的 image_url 部件。"""
    blocks: list[dict] = []
    if not isinstance(content, list):
        return blocks
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in ("input_image", "image_url"):
            continue
        url = item.get("image_url") or item.get("url") or ""
        if isinstance(url, dict):
            url = url.get("url", "")
        if not url:
            continue
        block: dict[str, Any] = {"type": "image_url", "image_url": {"url": url}}
        if item.get("detail"):
            block["image_url"]["detail"] = item["detail"]
        blocks.append(block)
    return blocks


def _convert_input(input_value: Any) -> list[dict]:
    """把 `input`（字符串 / 消息数组）翻成 chat 的 `messages`。"""
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if not isinstance(input_value, list):
        return []

    messages: list[dict] = []
    for item in input_value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type") or "message"

        if item_type == "message":
            role = item.get("role") or "user"
            # Responses 有 developer 角色，chat 里对应 system。
            if role == "developer":
                role = "system"
            text = text_of(item.get("content"))
            images = _image_blocks(item.get("content"))
            if images and role == "user":
                parts: list[dict] = []
                if text:
                    parts.append({"type": "text", "text": text})
                parts.extend(images)
                messages.append({"role": role, "content": parts})
            else:
                messages.append({"role": role, "content": text})
            continue

        if item_type == "function_call":
            # 上一轮模型发起的调用：chat 里挂到 assistant 的 tool_calls 上。
            call = {
                "id": item.get("call_id") or item.get("id") or new_id("call"),
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "{}",
                },
            }
            last = messages[-1] if messages else None
            if last and last.get("role") == "assistant" and "tool_calls" in last:
                last["tool_calls"].append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            continue

        if item_type == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "",
                "content": text_of(item.get("output")),
            })
            continue

        # 其它类型（reasoning / item_reference / computer_call …）上游没有对应物，丢弃。
    return messages


def _convert_tools(tools: Any) -> list[dict]:
    """Responses 的扁平 function 工具 → chat 的嵌套 function 工具。"""
    out: list[dict] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            # web_search / file_search / computer_use 这类托管工具上游没有，跳过。
            continue
        name = tool.get("name") or (tool.get("function") or {}).get("name")
        if not name:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description") or "",
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return out


def _convert_tool_choice(choice: Any) -> Any:
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict):
        if choice.get("type") == "function" and choice.get("name"):
            return {"type": "function", "function": {"name": choice["name"]}}
        return "auto"
    return None


def to_chat_request(body: dict) -> dict:
    """Responses 请求体 → chat/completions 请求体。

    `body` 是已经解析好的 JSON；返回一个新的 dict，不改动入参。
    """
    chat: dict[str, Any] = {
        "model": body.get("model") or "",
        "messages": _convert_input(body.get("input")),
        "stream": bool(body.get("stream")),
        # 网关不存对话；显式关掉，免得上游按额度记一条「状态保存」。
        "store": False,
    }

    if body.get("instructions"):
        # Responses 的 instructions 等价于 chat 的最前面一条 system。
        chat["messages"].insert(0, {"role": "system", "content": str(body["instructions"])})

    for src, dst in (("max_output_tokens", "max_tokens"), ("temperature", "temperature"),
                     ("top_p", "top_p")):
        if body.get(src) is not None:
            chat[dst] = body[src]

    if body.get("parallel_tool_calls") is not None:
        chat["parallel_tool_calls"] = bool(body["parallel_tool_calls"])

    tools = _convert_tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
        tool_choice = _convert_tool_choice(body.get("tool_choice"))
        if tool_choice is not None:
            chat["tool_choice"] = tool_choice

    # 响应格式：Responses 的 text.format 对应 chat 的 response_format。
    text_cfg = body.get("text")
    if isinstance(text_cfg, dict):
        fmt = text_cfg.get("format")
        if isinstance(fmt, dict):
            if fmt.get("type") == "json_schema":
                chat["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": fmt.get("name") or "output",
                        "schema": fmt.get("schema") or {},
                        **({"strict": fmt["strict"]} if "strict" in fmt else {}),
                    },
                }
            elif fmt.get("type") in ("json_object", "text"):
                if fmt["type"] == "json_object":
                    chat["response_format"] = {"type": "json_object"}
    return chat


# ---------------------------------------------------------------------------
# chat 响应 → Responses 响应
# ---------------------------------------------------------------------------
def _usage_object(usage: Any) -> dict:
    """chat 的 usage → Responses 的 usage（字段名不同，codex 会读这些）。"""
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    cached = 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = int(details.get("cached_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": total,
    }


def _message_item(text: str, status: str = "completed") -> dict:
    content: list[dict] = []
    if text:
        content.append({"type": "output_text", "text": text, "annotations": []})
    return {
        "id": new_id("msg"),
        "type": "message",
        "role": "assistant",
        "status": status,
        "content": content,
    }


def _function_call_item(call: dict) -> dict:
    fn = call.get("function") or {}
    return {
        "id": new_id("fc"),
        "type": "function_call",
        "status": "completed",
        "call_id": call.get("id") or new_id("call"),
        "name": fn.get("name") or "",
        "arguments": fn.get("arguments") or "{}",
    }


def to_response_object(chat: dict, *, model: str, response_id: str | None = None,
                       created_at: int | None = None) -> dict:
    """非流式：chat/completions 的响应体 → Responses 的响应体。"""
    choice = (chat.get("choices") or [{}])[0] if isinstance(chat, dict) else {}
    message = choice.get("message") or {}
    text = message.get("content") or ""
    if isinstance(text, list):
        text = text_of(text)

    output: list[dict] = []
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            output.append(_function_call_item(call))
    if text or not output:
        output.append(_message_item(text))

    finish = choice.get("finish_reason") or "stop"
    incomplete = None
    if finish == "length":
        incomplete = {"reason": "max_output_tokens"}

    return {
        "id": response_id or new_id("resp"),
        "object": "response",
        "created_at": created_at if created_at is not None else int(time.time()),
        "status": "incomplete" if incomplete else "completed",
        "model": chat.get("model") or model,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": _usage_object(chat.get("usage")),
        "error": None,
        "incomplete_details": incomplete,
        "instructions": None,
        "max_output_tokens": None,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "top_p": None,
        "truncation": "disabled",
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# 流式翻译
# ---------------------------------------------------------------------------
class StreamTranslator:
    """把上游 chat SSE 事件流翻译成 Responses 事件流。

    用法：
        tr = StreamTranslator(model="zai_auto")
        for event in tr.start(): yield event          # response.created / in_progress
        for chunk in chat_sse_chunks:                  # 上游一个个 SSE data
            for event in tr.feed(chunk): yield event
        for event in tr.finish(): yield event

    每个 `event` 已经是完整的 `data: {...}\\n\\n` 字节串，直接写进流即可。
    """

    def __init__(self, model: str, *, response_id: str | None = None,
                 created_at: int | None = None):
        self.model = model
        self.response_id = response_id or new_id("resp")
        self.created_at = created_at if created_at is not None else int(time.time())
        self._seq = 0
        self._output_index = 0
        self._text_item: dict | None = None      # 当前打开的 message 项
        self._text_buf: list[str] = []
        self._tool_items: dict[int, dict] = {}   # chat 的 tool_calls 下标 → 项状态
        self._usage: dict = {}
        self._finish_reason: str = ""
        self._finished = False

    # -- 事件构造 ---------------------------------------------------------
    def _event(self, name: str, payload: dict) -> bytes:
        payload = dict(payload)
        payload["type"] = name
        payload["sequence_number"] = self._seq
        self._seq += 1
        return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

    def _response_dict(self, status: str, output: list[dict] | None = None) -> dict:
        incomplete = {"reason": "max_output_tokens"} if self._finish_reason == "length" else None
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": output or [],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": _usage_object(self._usage) if status != "in_progress" else None,
            "error": None,
            "incomplete_details": incomplete if status == "incomplete" else None,
            "instructions": None,
            "max_output_tokens": None,
            "previous_response_id": None,
            "reasoning": {"effort": None, "summary": None},
            "store": False,
            "temperature": None,
            "text": {"format": {"type": "text"}},
            "top_p": None,
            "truncation": "disabled",
            "metadata": {},
        }

    # -- 生命周期 ---------------------------------------------------------
    @property
    def usage(self) -> dict:
        """上游最后给出的 usage（可能是空 dict，上游不保证每次都给）。"""
        return self._usage

    def start(self) -> Iterable[bytes]:
        """响应开始：created + in_progress。"""
        yield self._event("response.created", {"response": self._response_dict("in_progress")})
        yield self._event("response.in_progress", {"response": self._response_dict("in_progress")})

    def _open_text(self) -> Iterable[bytes]:
        if self._text_item is not None:
            return
        self._text_item = {
            "id": new_id("msg"),
            "index": self._output_index,
        }
        self._output_index += 1
        item = _message_item("", status="in_progress")
        item["id"] = self._text_item["id"]
        yield self._event("response.output_item.added", {
            "output_index": self._text_item["index"], "item": item,
        })
        yield self._event("response.content_part.added", {
            "item_id": self._text_item["id"], "output_index": self._text_item["index"],
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        })

    def _close_text(self) -> Iterable[bytes]:
        if self._text_item is None:
            return
        text = "".join(self._text_buf)
        self._text_buf.clear()
        item_id, index = self._text_item["id"], self._text_item["index"]
        yield self._event("response.output_text.done", {
            "item_id": item_id, "output_index": index, "content_index": 0, "text": text,
        })
        yield self._event("response.content_part.done", {
            "item_id": item_id, "output_index": index, "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        })
        yield self._event("response.output_item.done", {
            "output_index": index, "item": _message_item(text),
        })
        self._text_item = None

    def _tool_state(self, index: int) -> dict:
        state = self._tool_items.get(index)
        if state is None:
            state = {"id": new_id("fc"), "index": self._output_index,
                     "call_id": "", "name": "", "arguments": ""}
            self._output_index += 1
            self._tool_items[index] = state
        return state

    def _emit_tool_added(self, state: dict) -> bytes:
        return self._event("response.output_item.added", {
            "output_index": state["index"],
            "item": {
                "id": state["id"], "type": "function_call", "status": "in_progress",
                "call_id": state["call_id"], "name": state["name"], "arguments": "",
            },
        })

    def feed(self, chunk: dict) -> Iterable[bytes]:
        """喂一个上游 SSE 事件（已解析成 dict），吐 0..n 个 Responses 事件。"""
        if not isinstance(chunk, dict):
            return
        if isinstance(chunk.get("usage"), dict):
            self._usage = chunk["usage"]

        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}

            text = delta.get("content")
            if isinstance(text, list):
                text = text_of(text)
            if text:
                yield from self._open_text()
                self._text_buf.append(text)
                yield self._event("response.output_text.delta", {
                    "item_id": self._text_item["id"], "output_index": self._text_item["index"],
                    "content_index": 0, "delta": text,
                })

            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                state = self._tool_state(int(call.get("index") or 0))
                fn = call.get("function") or {}
                if call.get("id") and not state["call_id"]:
                    state["call_id"] = call["id"]
                if fn.get("name") and not state["name"]:
                    state["name"] = fn["name"]
                    yield from self._close_text()
                    yield self._emit_tool_added(state)
                args = fn.get("arguments")
                if args:
                    state["arguments"] += args
                    yield self._event("response.function_call_arguments.delta", {
                        "item_id": state["id"], "output_index": state["index"], "delta": args,
                    })

            if choice.get("finish_reason"):
                self._finish_reason = str(choice["finish_reason"])

    def finish(self) -> Iterable[bytes]:
        if self._finished:
            return
        self._finished = True

        yield from self._close_text()

        for state in sorted(self._tool_items.values(), key=lambda s: s["index"]):
            yield self._event("response.function_call_arguments.done", {
                "item_id": state["id"], "output_index": state["index"],
                "arguments": state["arguments"] or "{}",
            })
            yield self._event("response.output_item.done", {
                "output_index": state["index"],
                "item": {
                    "id": state["id"], "type": "function_call", "status": "completed",
                    "call_id": state["call_id"], "name": state["name"],
                    "arguments": state["arguments"] or "{}",
                },
            })

        status = "incomplete" if self._finish_reason == "length" else "completed"
        yield self._event("response.completed" if status == "completed" else "response.incomplete",
                          {"response": self._response_dict(status)})

    def fail(self, message: str) -> Iterable[bytes]:
        """上游中途报错：按 Responses 的语义发 response.failed。"""
        if self._finished:
            return
        self._finished = True
        resp = self._response_dict("failed")
        resp["error"] = {"code": "upstream_error", "message": message}
        yield self._event("response.failed", {"response": resp})


def parse_sse_line(line: bytes) -> dict | None:
    """把一行 `data: {...}` 解析成 dict；不是数据行 / 不完整就返回 None。"""
    line = line.strip()
    if not line.startswith(b"data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == b"[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
