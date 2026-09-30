"""HTTP server: OpenAI-compatible API endpoints.

Endpoints
    GET  /healthz                       liveness/version probe
    GET  /v1/models                     OpenAI model list
    POST /v1/chat/completions           OpenAI chat (stream + tools)
    POST /v1/responses                  OpenAI Responses (Codex CLI)
    GET  /v1beta/models                 Google model list
    POST /v1beta/models/{m}:generateContent | :streamGenerateContent
    GET  /ui                            built-in console
    /api/*                              console management API

Request handling notes
    * Bodies are size-capped (MAX_REQUEST_BODY_BYTES) and oversized requests are
      rejected with 413 before the JSON parser sees them.
    * CORS is opt-in via cors_origins; the console works same-origin without it.
    * State-changing /api requests must be same-origin JSON, which closes the
      form-POST/CSRF hole a wildcard CORS header used to leave open.
    * Streaming errors are reported inside the stream, so a truncated answer is
      never mistaken for a complete one.
"""
import hmac
import json
import re
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from . import __version__
from .config import CONFIG, get_int, get_list
from .gemini import (GenerationResult, generate, generate_stream,
                     generate_stream_events, log)
from .models import MODELS, resolve_model
from .multimodal import (ResponseTooLargeError, UnsafeURLError,
                         detect_image_mime, fetch_image_bytes, upload_image)
from .tools import (google_contents_to_prompt, messages_to_prompt,
                    parse_google_function_calls, parse_tool_calls)
from .webui import handle_api as webui_api
from .webui import handle_ui as webui_page

_BROWSER_UA = re.compile(r"Mozilla|Chrome|Safari|Edge|Firefox|Opera", re.IGNORECASE)


class RequestTooLarge(ValueError):
    """The request body exceeded the configured cap."""


def _usage(prompt: str, text: str) -> dict:
    """Rough token accounting (~4 characters per token)."""
    p = len(prompt or "") // 4
    c = len(text or "") // 4
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _emit_reasoning() -> bool:
    return bool(CONFIG.get("emit_reasoning"))


def _emit_images() -> bool:
    return bool(CONFIG.get("emit_generated_images", True))


def _socket_timeout():
    """Seconds of zero socket progress before the connection is dropped.

    Returns None when disabled (config value 0), which is what
    `socketserver` treats as "block forever". An unset or null field means
    "use the default" rather than "disable" -- switching the guard off has to
    be explicit, so a stray null cannot silently reopen the hole.
    """
    raw = CONFIG.get("client_socket_timeout_sec", 300)
    if raw is None:
        raw = 300
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = 300
    return seconds if seconds > 0 else None


def _upload_images(images: list) -> list:
    """Upload images and return their file references (None when there are none)."""
    if not images:
        return None
    limit = get_int("max_image_attachments", 8)
    if len(images) > limit:
        raise RuntimeError(f"too many image attachments ({len(images)} > {limit})")
    file_refs = []
    for item in images:
        if not (isinstance(item, tuple) and len(item) == 2):
            continue
        data, mime = item
        if isinstance(data, str):
            data = fetch_image_bytes(data)
            mime = mime or "image/png"
        if not data:
            raise RuntimeError("image fetch failed")
        mime = detect_image_mime(data, mime or "image/png")
        try:
            file_refs.append(upload_image(data, "image.png", mime or "image/png"))
        except Exception as e:
            raise RuntimeError(f"image upload failed: {e}") from e
    return file_refs if file_refs else None


class GeminiHandler(BaseHTTPRequestHandler):
    server_version = f"gemini-web2api/{__version__}"

    # ─── logging / plumbing ──────────────────────────────────────────────────

    def setup(self):
        """Apply the slow-client guard before any bytes are read.

        Without a socket timeout a client can open a connection, send half a
        header line and hold a worker thread forever -- a handful of those
        exhaust the thread pool and the API stops answering. The limit also
        covers writes, so a reader that stops draining an SSE stream is dropped
        rather than pinning a thread against a full socket buffer. Both are
        "no progress for N seconds", far longer than the gap between streamed
        deltas, so healthy clients are unaffected.
        """
        self.timeout = _socket_timeout()
        BaseHTTPRequestHandler.setup(self)

    def log_message(self, fmt, *args):
        # Skip the console's own traffic: /ui page loads and /api/* polling would
        # otherwise spam the log panel with a new line every few seconds.
        if self.path.startswith("/api/") or self.path.startswith("/ui"):
            return
        client_ip = self.client_address[0] if self.client_address else "-"
        log(f"{client_ip} {fmt % args}")

    def _cors_headers(self) -> dict:
        """Only echo origins the operator explicitly allow-listed."""
        origin = self.headers.get("Origin")
        if not origin:
            return {}
        allowed = [item for item in get_list("cors_origins") if item]
        if origin in allowed:
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        return {}

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for name, value in self._cors_headers().items():
            self.send_header(name, value)
        if status == 413:
            # The client may still be writing; do not try to reuse the socket.
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        for name, value in self._cors_headers().items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True

    def _write_sse(self, payload, event=None):
        prefix = f"event: {event}\n" if event else ""
        self.wfile.write(f"{prefix}data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

    def _sse_upstream_error(self, message):
        """Report a mid-stream failure inside the stream itself.

        Without this the client sees a clean end of stream and treats a
        truncated answer as a complete one.
        """
        self._write_sse({"error": {"message": str(message), "type": "upstream_error",
                                   "code": "upstream_stream_failed"}})

    def _parse_body(self, body: bytes):
        try:
            value = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _body_limit(self) -> int:
        return get_int("max_request_body_bytes", 64 * 1024 * 1024)

    def _read_request_body(self) -> bytes:
        limit = self._body_limit()
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if "chunked" in transfer_encoding.lower():
            chunks = []
            total = 0
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break
                size_text = size_line.split(b";", 1)[0].strip()
                try:
                    size = int(size_text, 16)
                except ValueError:
                    raise ValueError("invalid chunked request body")
                if size == 0:
                    while True:
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > limit:
                    raise RequestTooLarge(
                        f"request body exceeds the {limit} byte limit; "
                        "reduce the payload or raise max_request_body_bytes")
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            raise ValueError("invalid Content-Length")
        if length < 0:
            raise ValueError("invalid Content-Length")
        if length > limit:
            raise RequestTooLarge(
                f"request body exceeds the {limit} byte limit; "
                "reduce the payload or raise max_request_body_bytes")
        return self.rfile.read(length) if length else b""

    # ─── auth / origin guards ────────────────────────────────────────────────

    def _key_matches(self, candidate) -> bool:
        """Constant-time comparison against every configured key.

        Comparison happens on UTF-8 bytes because hmac.compare_digest() rejects
        str inputs containing non-ASCII characters, and API keys are whatever the
        operator pasted into the config.
        """
        keys = [str(key) for key in (CONFIG.get("api_keys") or []) if str(key)]
        if not keys:
            return True
        if not candidate:
            return False
        candidate_bytes = str(candidate).encode("utf-8", "replace")
        matched = False
        for key in keys:
            # Compare every key so the timing does not reveal how many were tried.
            matched |= hmac.compare_digest(key.encode("utf-8", "replace"), candidate_bytes)
        return matched

    def _authorized(self):
        keys = [key for key in (CONFIG.get("api_keys") or []) if key]
        if not keys:
            return True
        # Authorization: Bearer <key>
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and self._key_matches(auth[7:]):
            return True
        # header keys (OpenAI x-api-key / Google x-goog-api-key)
        for header in ("x-api-key", "x-goog-api-key"):
            if self._key_matches(self.headers.get(header, "")):
                return True
        # query param ?key= (Gemini CLI native style)
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("key=") and self._key_matches(pair[4:]):
                    return True
        return False

    def _reject_unauthorized(self):
        client_ip = self.client_address[0] if self.client_address else "-"
        log(f"401 unauthorized: {client_ip} {self.command} {self.path.split('?')[0]}", level="warning")
        self.send_json({"error": {"message": "invalid api key"}}, 401)

    def _origin_is_self(self, origin) -> bool:
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        return bool(host) and parsed.netloc.lower() == host

    def _console_guard(self, method: str):
        """Reject cross-site and non-JSON console mutations.

        A cross-origin HTML form can POST a body with Content-Type
        text/plain and no CORS preflight, which would otherwise be enough to
        rewrite the config, so both the Origin and the media type are checked.
        """
        origin = self.headers.get("Origin")
        if origin:
            allowed = [item for item in get_list("cors_origins") if item]
            if origin not in allowed and not self._origin_is_self(origin):
                return 403, "cross-origin request rejected"
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                return 415, "Content-Type must be application/json"
        return None

    # ─── HTTP verbs ──────────────────────────────────────────────────────────

    def do_OPTIONS(self):
        headers = {"Access-Control-Allow-Methods": "GET, POST, OPTIONS"}
        origin = self.headers.get("Origin")
        if origin:
            allowed = [item for item in get_list("cors_origins") if item]
            if origin not in allowed:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, x-api-key, x-goog-api-key"
        self.send_response(204)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            path = self.path.split("?")[0]
            if path == "/":
                # Browsers land on the console; scripts keep the JSON status.
                if _BROWSER_UA.search(self.headers.get("User-Agent", "")):
                    self.send_response(302)
                    self.send_header("Location", "/ui")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            elif path == "/healthz":
                self.send_json({"status": "ok", "version": __version__,
                                "models": len(MODELS)})
                return
            elif path == "/ui":
                webui_page(self)
                return
            elif path.startswith("/api/"):
                if not self._authorized():
                    self._reject_unauthorized()
                    return
                guard = self._console_guard("GET")
                if guard:
                    self.send_json({"error": {"message": guard[1]}}, guard[0])
                    return
                webui_api(self, "GET", path)
                return
            if self.path.startswith("/v1") and not self._authorized():
                self._reject_unauthorized()
                return
            if path == "/v1/models":
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif path.startswith("/v1beta/models"):
                self.send_json({"models": [
                    {"name": f"models/{n}", "displayName": n, "description": c["desc"],
                     "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]}
                    for n, c in MODELS.items()
                ]})
            elif path == "/":
                self.send_json({"status": "ok", "version": __version__, "models": list(MODELS.keys())})
            else:
                self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        try:
            path = self.path.split("?")[0]
            if path.startswith("/api/"):
                if not self._authorized():
                    self._reject_unauthorized()
                    return
                guard = self._console_guard("POST")
                if guard:
                    self.send_json({"error": {"message": guard[1]}}, guard[0])
                    return
                webui_api(self, "POST", path)
                return
            if self.path.startswith("/v1") and not self._authorized():
                self._reject_unauthorized()
                return
            body = self._read_request_body()
            if path == "/v1/chat/completions":
                self._handle_chat(body)
            elif path == "/v1/responses":
                self._handle_responses(body)
            elif ":streamGenerateContent" in path:
                self._handle_google_generate(body, stream=True)
            elif ":generateContent" in path:
                self._handle_google_generate(body, stream=False)
            else:
                self.send_json({"error": "not found"}, 404)
        except RequestTooLarge as e:
            log(f"413 request too large: {e}", level="warning")
            self.send_json({"error": {"message": str(e), "type": "request_too_large"}}, 413)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}", level="error")
            try:
                self.send_json({"error": {"message": str(e)}}, 500)
            except Exception:
                pass

    def do_DELETE(self):
        try:
            path = self.path.split("?")[0]
            if path.startswith("/api/"):
                if not self._authorized():
                    self._reject_unauthorized()
                    return
                guard = self._console_guard("DELETE")
                if guard:
                    self.send_json({"error": {"message": guard[1]}}, guard[0])
                    return
                webui_api(self, "DELETE", path)
                return
            self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"DELETE error: {e}", level="error")

    # ─── /v1/chat/completions ────────────────────────────────────────────────

    def _handle_chat(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(req.get("messages", []), tools, tool_choice)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty prompt"}}, 400)
            return

        stream = req.get("stream", False)
        log(f"chat 请求 model={model_name} stream={bool(stream)} "
            f"messages={len(req.get('messages', []))} prompt={len(prompt)}字")
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        try:
            file_refs = _upload_images(images)
        except (RuntimeError, UnsafeURLError, ResponseTooLargeError) as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        if stream and (not tools or tool_choice == "none"):
            self._start_sse()
            self._write_sse({
                "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            })
            failed = False
            try:
                for kind, delta in _stream_events(prompt, model_id, think_mode, file_refs, extra_fields):
                    if kind == "thinking" and not _emit_reasoning():
                        continue
                    payload = {"reasoning_content": delta} if kind == "thinking" else {"content": delta}
                    self._write_sse({
                        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                        "model": model_name,
                        "choices": [{"index": 0, "delta": payload, "finish_reason": None}],
                    })
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as e:
                failed = True
                log(f"Stream error: {e}", level="error")
                try:
                    self._sse_upstream_error(e)
                except (BrokenPipeError, ConnectionResetError):
                    return
            try:
                self._write_sse({
                    "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {},
                                 "finish_reason": None if failed else "stop"}],
                })
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        captured = []
        try:
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields,
                            capture=captured)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return
        result = captured[0] if captured else GenerationResult(text=text)

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)
        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if _emit_reasoning() and result.thoughts:
            msg["reasoning_content"] = result.thoughts
        if _emit_images() and result.images:
            msg["images"] = result.images
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            self._start_sse()
            self._write_sse({
                "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "delta": msg, "finish_reason": finish}],
            })
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_json({
                "id": cid, "object": "chat.completion", "created": int(time.time()),
                "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": _usage(prompt, text or ""),
            })

    # ─── /v1/responses (Codex CLI) ───────────────────────────────────────────

    def _handle_responses(self, body: bytes):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        model_name, model_id, think_mode, err, extra_fields = resolve_model(
            req.get("model", CONFIG["default_model"]))
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        messages = self._responses_messages(req)
        tools = req.get("tools")
        if tools:
            tools = [{"type": "function", "function": {"name": t["name"],
                                                       "description": t.get("description", ""),
                                                       "parameters": t.get("parameters", {})}}
                     if t.get("type") == "function" and "function" not in t else t
                     for t in tools]

        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(messages, tools, tool_choice)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty input"}}, 400)
            return

        try:
            file_refs = _upload_images(images)
            # No capture= here on purpose: the Responses output schema has no
            # slot for reasoning summaries or generated image URLs, and adding
            # unknown item types risks breaking the CLI clients that parse this
            # endpoint. Reasoning and images are reported by the other two
            # endpoints instead.
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        except (RuntimeError, UnsafeURLError, ResponseTooLargeError) as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return

        tool_calls = None
        if tools and text and tool_choice != "none":
            text, tool_calls = parse_tool_calls(text)

        rid = f"resp_{uuid.uuid4().hex[:16]}"
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tool_call in tool_calls:
                output.append({"type": "function_call", "id": tool_call["id"],
                               "call_id": tool_call["id"],
                               "name": tool_call["function"]["name"],
                               "arguments": tool_call["function"]["arguments"],
                               "status": "completed"})
        if text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant",
                           "status": "completed",
                           "content": [{"type": "output_text", "text": text or "",
                                        "annotations": []}]})

        if req.get("stream"):
            self._start_sse()
            sequence_number = 0

            def emit(event_type, **fields):
                nonlocal sequence_number
                sequence_number += 1
                event = {"type": event_type, "sequence_number": sequence_number, **fields}
                self.wfile.write(
                    f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
                )

            usage = {"input_tokens": len(prompt) // 4,
                     "output_tokens": len(text or "") // 4,
                     "total_tokens": (len(prompt) + len(text or "")) // 4}
            base_response = {"id": rid, "object": "response",
                             "created_at": int(time.time()), "model": model_name}
            emit("response.created", response={**base_response, "status": "in_progress",
                                               "output": [], "usage": None})
            emit("response.in_progress", response={**base_response, "status": "in_progress",
                                                   "output": [], "usage": None})
            for output_index, item in enumerate(output):
                if item["type"] == "function_call":
                    emit("response.output_item.added", output_index=output_index,
                         item={"type": "function_call", "id": item["id"],
                               "call_id": item["call_id"], "name": item["name"],
                               "arguments": "", "status": "in_progress"})
                    emit("response.function_call_arguments.delta", item_id=item["id"],
                         output_index=output_index, delta=item["arguments"])
                    emit("response.function_call_arguments.done", item_id=item["id"],
                         output_index=output_index, arguments=item["arguments"])
                    emit("response.output_item.done", output_index=output_index, item=item)
                elif item["type"] == "message":
                    emit("response.output_item.added", output_index=output_index,
                         item={"type": "message", "id": item["id"], "role": "assistant",
                               "status": "in_progress", "content": []})
                    for content_index, content_part in enumerate(item["content"]):
                        event_fields = {"item_id": item["id"], "output_index": output_index,
                                        "content_index": content_index}
                        emit("response.content_part.added", **event_fields,
                             part={"type": "output_text", "text": "", "annotations": []})
                        emit("response.output_text.delta", **event_fields,
                             delta=content_part["text"])
                        emit("response.output_text.done", **event_fields,
                             text=content_part["text"])
                        emit("response.content_part.done", **event_fields, part=content_part)
                    emit("response.output_item.done", output_index=output_index, item=item)
            emit("response.completed", response={**base_response, "status": "completed",
                                                 "output": output, "usage": usage})
            self.wfile.flush()
        else:
            self.send_json({"id": rid, "object": "response", "created_at": int(time.time()),
                            "status": "completed", "model": model_name, "output": output,
                            "usage": {"input_tokens": len(prompt) // 4,
                                      "output_tokens": len(text or "") // 4,
                                      "total_tokens": (len(prompt) + len(text or "")) // 4}})

    @staticmethod
    def _responses_messages(req: dict) -> list:
        """Flatten a Responses `input` payload into chat-style messages."""
        input_items = req.get("input", [])
        messages = []
        if req.get("instructions"):
            messages.append({"role": "system", "content": req["instructions"]})
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
            return messages
        if not isinstance(input_items, list):
            return messages
        for item in input_items:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                if item.get("type") == "function_call_output":
                    messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                                     "name": item.get("name", ""),
                                     "content": item.get("output", "")})
                elif item.get("type") in ("input_text", "input_image", "image"):
                    messages.append({"role": "user", "content": [item]})
                elif item.get("role") == "assistant" or (item.get("type") == "message"
                                                         and item.get("role") == "assistant"):
                    content = item.get("content", [])
                    text_acc, tool_calls = "", []
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict):
                                if part.get("type") == "output_text":
                                    text_acc += part.get("text", "")
                                elif part.get("type") == "function_call":
                                    tool_calls.append(part)
                    elif isinstance(content, str):
                        text_acc = content
                    message = {"role": "assistant", "content": text_acc or None}
                    if tool_calls:
                        message["tool_calls"] = [
                            {"id": call.get("call_id", f"call_{i}"), "type": "function",
                             "function": {"name": call.get("name", ""),
                                          "arguments": call.get("arguments", "{}")}}
                            for i, call in enumerate(tool_calls)]
                    messages.append(message)
                else:
                    messages.append({"role": item.get("role", "user"),
                                     "content": item.get("content", "")})
        return messages

    # ─── /v1beta/models (Google Gemini CLI) ──────────────────────────────────

    def _handle_google_generate(self, body: bytes, stream: bool):
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        match = re.match(r'/v1beta/models/([^:?]+)', self.path)
        model_name = match.group(1) if match else CONFIG["default_model"]
        model_name, model_id, think_mode, err, extra_fields = resolve_model(model_name)
        if err:
            self.send_json({"error": {"message": err}}, 400)
            return

        tool_config = req.get("toolConfig", {})
        fc_mode = tool_config.get("functionCallingConfig", {}).get("mode", "AUTO")
        has_tools = bool(req.get("tools")) and fc_mode != "NONE"
        prompt, images = google_contents_to_prompt(req)
        if not prompt.strip():
            self.send_json({"error": {"message": "empty content"}}, 400)
            return

        try:
            file_refs = _upload_images(images)
        except (RuntimeError, UnsafeURLError, ResponseTooLargeError) as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return
        log(f"Google API: model={model_name} stream={stream} tools={has_tools} prompt_len={len(prompt)}")

        if stream and not has_tools:
            self._start_sse()
            full_text = ""
            try:
                for kind, delta in _stream_events(prompt, model_id, think_mode, file_refs, extra_fields):
                    if kind != "text" or not delta:
                        continue
                    full_text += delta
                    self._write_sse({
                        "candidates": [{"content": {"parts": [{"text": delta}], "role": "model"},
                                        "index": 0}],
                        "modelVersion": model_name,
                    })
                self._write_sse({
                    "candidates": [{"finishReason": "STOP", "index": 0}],
                    "usageMetadata": {
                        "promptTokenCount": len(prompt) // 4,
                        "candidatesTokenCount": len(full_text) // 4,
                        "totalTokenCount": (len(prompt) + len(full_text)) // 4,
                    },
                    "modelVersion": model_name,
                })
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as e:
                log(f"Google stream error: {e}", level="error")
                try:
                    self._sse_upstream_error(e)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return

        captured = []
        try:
            text = generate(prompt, model_id, think_mode, file_refs, extra_fields,
                            capture=captured)
        except Exception as e:
            self.send_json({"error": {"message": f"upstream error: {e}"}}, 502)
            return
        result = captured[0] if captured else GenerationResult(text=text)

        if not text:
            log("Warning: empty response from Gemini", level="warning")

        response_parts = []
        if has_tools and text:
            clean, function_calls = parse_google_function_calls(text)
            if function_calls:
                if clean:
                    response_parts.append({"text": clean})
                for call in function_calls:
                    response_parts.append({"functionCall": {"name": call["name"],
                                                            "args": call["args"]}})
            else:
                response_parts.append({"text": text})
        else:
            response_parts.append({
                "text": text or "I apologize, but I was unable to generate a response. Please try again."})

        if _emit_images():
            for image in result.generated_images:
                response_parts.append({"fileData": {"fileUri": image["url"],
                                                    "mimeType": "image/png"}})

        candidate = {"content": {"parts": response_parts, "role": "model"},
                     "finishReason": "STOP", "index": 0}
        if _emit_reasoning() and result.thoughts:
            candidate["thoughts"] = [{"text": result.thoughts}]
        usage = {"promptTokenCount": len(prompt) // 4,
                 "candidatesTokenCount": len(text or "") // 4,
                 "totalTokenCount": (len(prompt) + len(text or "")) // 4}
        response_obj = {"candidates": [candidate], "usageMetadata": usage,
                        "modelVersion": model_name}

        if stream:
            self._start_sse()
            self._write_sse(response_obj)
        else:
            self.send_json(response_obj)


def _stream_events(prompt, model_id, think_mode, file_refs, extra_fields):
    """Yield ("text"|"thinking", delta) pairs.

    `generate_stream` is kept as the text-only entry point so that reasoning
    output stays opt-in.
    """
    if _emit_reasoning():
        return generate_stream_events(prompt, model_id, think_mode, file_refs, extra_fields)
    return (("text", delta) for delta in generate_stream(prompt, model_id, think_mode,
                                                         file_refs, extra_fields))


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
