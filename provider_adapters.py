import base64
import hashlib
import io
import json
import mimetypes
import os
import shlex
import subprocess
import time
from typing import Any, Dict, Optional

from mcp_proxy_bridge import decode_mcp_response


# ---------------------------------------------------------------------------
# Image Files API cache helpers
# These two functions let us persist the mapping from an image's content hash
# to the Anthropic file_id we received when we first uploaded it.  On the
# second call with the same image bytes (even if the path changed) we reuse
# the existing file_id rather than uploading again.
# ---------------------------------------------------------------------------

def _load_image_file_id(cache_path: str, content_hash: str) -> Optional[str]:
    """Return a previously-uploaded Anthropic file_id for this image hash, or None."""
    if not os.path.exists(cache_path):
        return None
    with open(cache_path, "r", encoding="utf-8") as fh:
        return json.load(fh).get(content_hash)


def _save_image_file_id(cache_path: str, content_hash: str, file_id: str) -> None:
    """Persist a content_hash → file_id mapping to disk."""
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    data: Dict[str, str] = {}
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    data[content_hash] = file_id
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

PROVIDER_ENV_DEFAULTS: Dict[str, Dict[str, str]] = {
    "openai": {"api_key": "OPENAI_API_KEY"},
    "deepseek": {"api_key": "DEEPSEEK_API_KEY", "base_url": "DEEPSEEK_BASE_URL"},
    "baseten": {"api_key": "BASETEN_API_KEY"},
}

MODEL_COST_ESTIMATES: Dict[str, float] = {
    "deepseek-v4-flash": 0.000006,
    "deepseek-v4-pro": 0.000018,
}

OPENAI_COMPATIBLE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "local": {
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "mistral-7b-instruct",
        "timeout_seconds": 60.0,
        "api_key": "local-llama",
        "health_url": "http://127.0.0.1:8080/health",
        "root_url": "http://127.0.0.1:8080/",
        "bin_path": os.path.expanduser("~/llama.cpp/build/bin/llama-server"),
        "model_path": os.path.expanduser("~/localai/models/mistral/mistral"),
        "host": "0.0.0.0",
        "port": 8080,
        "threads": 4,
        "context_size": 4096,
        "batch_size": 256,
        "gpu_layers": 28,
        "log_dir": os.path.expanduser("~/.cache/llama-run"),
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "timeout_seconds": 60.0,
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "timeout_seconds": 60.0,
    },
    "baseten": {
        "base_url": "https://inference.baseten.co/v1",
        "model": "openai/gpt-oss-120b",
        "timeout_seconds": 60.0,
    },
    "dream_rest": {
        "base_url": "http://127.0.0.1:8181",
        "timeout_seconds": 30.0,
        "health_url": "http://127.0.0.1:8181/",
        "token_file": "/home/gross/dream/dream_tokens",
        "token_env_var": "DREAM_API_TOKEN",
        "source_agent": "ai-broker",
    },
}

def _safe_preview(value: Any, limit: int = 160) -> str:
    text = str(value)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _extract_chat_message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
            else:
                text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
        if parts:
            return "\n".join(parts)
    for attr in ("text",):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _openai_response_debug(response: Any) -> Dict[str, Any]:
    debug: Dict[str, Any] = {
        "response_object_type": type(response).__name__,
        "top_level_result_keys": [],
        "choices": [],
    }
    if hasattr(response, "model_fields_set"):
        try:
            debug["top_level_result_keys"] = sorted(str(k) for k in response.model_fields_set)
        except Exception:
            pass
    elif hasattr(response, "__dict__"):
        debug["top_level_result_keys"] = sorted(str(k) for k in vars(response).keys() if not str(k).startswith("_"))

    choices = getattr(response, "choices", None) or []
    for choice in choices[:3]:
        choice_info: Dict[str, Any] = {"choice_object_type": type(choice).__name__}
        message = getattr(choice, "message", None)
        if message is not None:
            content = getattr(message, "content", None)
            likely_fields = {}
            for field in ("content", "text", "reasoning_content", "refusal"):
                if hasattr(message, field):
                    value = getattr(message, field)
                    likely_fields[field] = {
                        "type": type(value).__name__,
                        "empty": value in (None, "", [], {}),
                    }
                    if value not in (None, "", [], {}) and field not in {"reasoning_content", "refusal"}:
                        likely_fields[field]["preview"] = _safe_preview(value)
            choice_info["message"] = {
                "message_object_type": type(message).__name__,
                "likely_text_bearing_fields": likely_fields,
            }
        for field in ("finish_reason", "stop_reason"):
            if hasattr(choice, field):
                choice_info[field] = getattr(choice, field)
        debug["choices"].append(choice_info)

    usage = getattr(response, "usage", None)
    if usage is not None:
        usage_fields = {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"):
            if hasattr(usage, field):
                usage_fields[field] = getattr(usage, field)
        debug["usage"] = usage_fields
    return debug


def _payload_text(task: Dict[str, Any]) -> str:
    payload = task.get("payload", {})
    if isinstance(payload, dict):
        if "text" in payload:
            return str(payload["text"])
        if "image_path" in payload:
            return str(payload["image_path"])
        if "command" in payload:
            return str(payload["command"])
    return str(payload)


def estimate_cost(provider: str, task: Dict[str, Any]) -> float:
    if provider == "fixture":
        return 0.0
    text = _payload_text(task)
    payload = task.get("payload", {}) if isinstance(task.get("payload"), dict) else {}
    length = max(1, len(text))
    unit_count = max(
        1,
        int(
            payload.get("unit_count")
            or payload.get("frame_count")
            or payload.get("batch_size")
            or payload.get("sample_count")
            or 1
        ),
    )
    model = str(payload.get("model") or OPENAI_COMPATIBLE_DEFAULTS.get(provider, {}).get("model", ""))
    base = MODEL_COST_ESTIMATES.get(model, {
        "local": 0.0,
        "openai": 0.00002,
        "deepseek": 0.000006,
        "baseten": 0.000015,
        "anthropic": 0.000025,
        "google_vision": 0.001,
        "shell": 0.0,
        "mcp_openbrain": 0.0,   # MCP server costs are borne by the remote service
        "dream_rest": 0.0,
    }.get(provider, 0.00001))

    if task.get("type") == "ocr" and provider == "google_vision":
        return round((0.001 + (0.000001 * length)) * unit_count, 6)

    # Vision tasks via Anthropic include a fixed per-image token cost on top of
    # the prompt text cost (roughly 1 600 tokens ≈ $0.008 for a typical image).
    if task.get("intent") == "vision" and provider == "anthropic":
        return round((0.008 + 0.000025 * length) * unit_count, 6)

    return round(base * length * unit_count, 6)


# ---------------------------------------------------------------------------
# Provider dispatch
# policy is threaded in from AIBroker so MCP adapters can read server config.
# ---------------------------------------------------------------------------

def _load_provider_credentials(
    provider: str,
    policy: Optional[Dict[str, Any]],
) -> Optional[str]:
    credentials = (policy or {}).get("provider_credentials", {})
    config = credentials.get(provider)
    if not config:
        return None

    env_var = str(config.get("env_var", "")).strip()
    key_file = str(config.get("key_file", "")).strip()
    if env_var and os.getenv(env_var):
        return None
    if not key_file:
        return None

    key_path = os.path.expanduser(key_file)
    try:
        with open(key_path, "r", encoding="utf-8") as fh:
            secret = fh.read().strip()
    except OSError as exc:
        return f"Cannot read credential file '{key_file}' for provider '{provider}': {exc}"

    if not secret:
        return f"Credential file '{key_file}' for provider '{provider}' is empty"

    os.environ[env_var] = secret
    return None


def _provider_runtime_config(
    provider: str,
    policy: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    defaults = OPENAI_COMPATIBLE_DEFAULTS.get(provider, {})
    configured = (policy or {}).get("provider_settings", {}).get(provider, {})
    merged = {**defaults, **configured}
    env_defaults = PROVIDER_ENV_DEFAULTS.get(provider, {})
    base_url_env = env_defaults.get("base_url")
    if base_url_env and os.getenv(base_url_env):
        merged["base_url"] = os.getenv(base_url_env)

    if "timeout_seconds" in merged:
        try:
            merged["timeout_seconds"] = float(merged["timeout_seconds"])
        except (TypeError, ValueError):
            merged["timeout_seconds"] = defaults.get("timeout_seconds", 60.0)
    return merged


def _load_exported_token(token_file: str, env_var: str) -> Optional[str]:
    path = os.path.expanduser(token_file)
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() != env_var:
                    continue
                parsed = shlex.split(f"x={value.strip()}")
                if not parsed or "=" not in parsed[0]:
                    return None
                return parsed[0].split("=", 1)[1]
    except OSError:
        return None
    except ValueError:
        return None

    return None


def _local_endpoint_healthy(runtime: Dict[str, Any]) -> bool:
    urls = [
        str(runtime.get("models_url", "")).strip(),
        str(runtime.get("health_url", "")).strip(),
        str(runtime.get("root_url", "")).strip(),
    ]
    try:
        import httpx
    except ImportError:
        return False

    timeout = min(float(runtime.get("timeout_seconds", 60.0)), 5.0)
    with httpx.Client(timeout=timeout) as client:
        for url in urls:
            if not url:
                continue
            try:
                response = client.get(url)
                if url.endswith("/v1/models"):
                    if response.status_code == 200:
                        return True
                elif response.status_code < 500:
                    return True
            except Exception:
                continue
    return False


def _ensure_local_server(runtime: Dict[str, Any]) -> Optional[str]:
    if _local_endpoint_healthy(runtime):
        return None

    bin_path = os.path.expanduser(str(runtime.get("bin_path", "")).strip())
    model_path = os.path.expanduser(str(runtime.get("model_path", "")).strip())
    if not bin_path or not os.path.exists(bin_path):
        return f"Local provider binary not found: {bin_path or runtime.get('bin_path', '')}"
    if not model_path or not os.path.exists(model_path):
        return f"Local provider model not found: {model_path or runtime.get('model_path', '')}"

    log_dir = os.path.expanduser(str(runtime.get("log_dir", "")).strip() or "~/.cache/llama-run")
    os.makedirs(log_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_file = os.path.join(log_dir, f"llama-server-{stamp}.log")

    args = [
        bin_path,
        "-m", model_path,
        "-t", str(int(runtime.get("threads", 4))),
        "-c", str(int(runtime.get("context_size", 4096))),
        "-b", str(int(runtime.get("batch_size", 256))),
        "-ngl", str(int(runtime.get("gpu_layers", 28))),
        "--host", str(runtime.get("host", "0.0.0.0")),
        "--port", str(int(runtime.get("port", 8080))),
    ]

    try:
        with open(log_file, "w", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                args,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError as exc:
        return f"Failed to launch local provider: {exc}"

    pid_file = f"{log_file}.pid"
    with open(pid_file, "w", encoding="utf-8") as fh:
        fh.write(str(process.pid))

    for _ in range(90):
        if _local_endpoint_healthy(runtime):
            return None
        if process.poll() is not None:
            try:
                with open(log_file, "r", encoding="utf-8") as fh:
                    tail = fh.readlines()[-20:]
                detail = "".join(tail).strip()
            except OSError:
                detail = f"exit code {process.returncode}"
            return f"Local provider exited during startup: {detail}"
        time.sleep(1)

    return f"Local provider health check failed after startup; see {log_file}"

def call_provider(
    provider: str,
    task: Dict[str, Any],
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if provider == "fixture":
        from fixture_provider import fetch_fixture
        return fetch_fixture(task, policy or {})
    credential_error = _load_provider_credentials(provider, policy)
    if credential_error:
        return {"ok": False, "error": credential_error}
    if provider == "shell":
        return _call_shell(task)
    if provider == "local":
        return _call_local(task, policy or {})
    if provider in {"openai", "deepseek", "baseten"}:
        return _call_openai_compatible(provider, task, policy or {})
    if provider == "dream_rest":
        return _call_dream_rest(task, policy or {})
    if provider == "anthropic":
        return _call_anthropic(task)
    if provider == "google_vision":
        return _call_google_vision_stub(task)
    # Any provider whose name starts with "mcp_" is routed to the MCP adapter.
    # The suffix (e.g. "openbrain") is the key in policy["mcp_servers"].
    if provider.startswith("mcp_"):
        server_key = provider[len("mcp_"):]
        return _call_mcp(task, policy or {}, server_key)
    return {"ok": False, "error": f"Unknown provider '{provider}'"}


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

def _call_shell(task: Dict[str, Any]) -> Dict[str, Any]:
    command = str(task.get("payload", {}).get("command", "")).strip()
    cwd = task.get("cwd", ".")
    if not command:
        return {"ok": False, "error": "Missing shell command"}

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return {"ok": False, "error": f"Cannot parse shell command: {exc}"}
    if not argv:
        return {"ok": False, "error": "Missing shell command"}

    result = subprocess.run(
        argv,
        shell=False,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {
        "ok": True,
        "result": {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
        "metadata": {
            "cwd": os.path.abspath(cwd),
            "intent": task.get("intent"),
            "argv": argv,
        },
    }


# ---------------------------------------------------------------------------
# Local stub (llama.cpp placeholder)
# ---------------------------------------------------------------------------

def _call_local(task: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    runtime = _provider_runtime_config("local", policy)
    ensure_error = _ensure_local_server(runtime)
    if ensure_error:
        return {"ok": False, "error": ensure_error}
    return _call_openai_compatible("local", task, policy)


# ---------------------------------------------------------------------------
# OpenAI-compatible chat providers
# ---------------------------------------------------------------------------

def _call_openai_compatible(
    provider: str,
    task: Dict[str, Any],
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    try:
        from openai import OpenAI
        from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError, RateLimitError
    except ImportError:
        return {"ok": False, "error": "openai package not installed. Run: pip install openai"}

    intent = task.get("intent")
    text = _payload_text(task)
    payload = task.get("payload") or {}
    runtime = _provider_runtime_config(provider, policy)
    model = str(payload.get("model") or runtime.get("model") or OPENAI_COMPATIBLE_DEFAULTS[provider]["model"])
    base_url = str(runtime.get("base_url") or OPENAI_COMPATIBLE_DEFAULTS[provider]["base_url"])
    timeout = float(runtime.get("timeout_seconds") or OPENAI_COMPATIBLE_DEFAULTS[provider]["timeout_seconds"])

    credentials = policy.get("provider_credentials", {}).get(provider, {})
    env_var = str(credentials.get("env_var") or PROVIDER_ENV_DEFAULTS.get(provider, {}).get("api_key", "")).strip()
    api_key = os.getenv(env_var) if env_var else None
    if not api_key:
        api_key = str(runtime.get("api_key", "")).strip() or None
    if not api_key:
        return {"ok": False, "error": f"{env_var or provider.upper() + '_API_KEY'} is not set"}

    system_prompts = {
        "summarize": "You are a precise summarization assistant. Produce concise, accurate summaries that capture the key points of the provided text.",
        "analyze": "You are a thorough analytical assistant. Examine the provided content carefully and deliver structured, insightful analysis.",
        "classify": "You are a classification assistant. Categorize the provided content accurately and explain your classification briefly.",
    }
    messages = []
    system = system_prompts.get(intent)
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": text})

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
    max_tokens = int(payload.get("max_tokens", 1000))
    temperature = float(payload.get("temperature", 0.2))

    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except AuthenticationError:
        return {"ok": False, "error": f"{provider} API key invalid or missing"}
    except RateLimitError:
        return {"ok": False, "error": f"{provider} rate limit reached; retry later"}
    except APITimeoutError:
        return {"ok": False, "error": f"{provider} timeout; retry later"}
    except APIConnectionError:
        return {"ok": False, "error": f"{provider} network error; retry later"}
    except APIStatusError as exc:
        return {"ok": False, "error": f"{provider} API error {exc.status_code}: {exc.message}"}

    response_text = ""
    if response.choices:
        response_text = _extract_chat_message_text(response.choices[0].message)

    metadata = {
        "model": model,
        "provider_name": provider,
        "base_url": base_url,
    }
    if getattr(response, "usage", None):
        metadata["input_tokens"] = getattr(response.usage, "prompt_tokens", 0)
        metadata["output_tokens"] = getattr(response.usage, "completion_tokens", 0)
        metadata["total_tokens"] = getattr(response.usage, "total_tokens", 0)
    if not response_text:
        metadata["response_debug"] = _openai_response_debug(response)

    return {
        "ok": True,
        "result": {
            "intent": intent,
            "text": response_text,
        },
        "metadata": metadata,
    }


def _call_dream_rest(task: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    try:
        import httpx
    except ImportError:
        return {"ok": False, "error": "httpx package not installed. Run: pip install httpx"}

    if task.get("intent") != "store_milestone":
        return {"ok": False, "error": f"Unsupported Dream intent '{task.get('intent')}'"}

    payload = task.get("payload") or {}
    summary = str(payload.get("summary", "")).strip()
    if not summary:
        return {"ok": False, "error": "Dream milestone payload requires 'summary'"}

    runtime = _provider_runtime_config("dream_rest", policy)
    base_url = str(runtime.get("base_url", "")).rstrip("/")
    if not base_url:
        return {"ok": False, "error": "Dream REST base_url is not configured"}

    timeout = float(runtime.get("timeout_seconds", 30.0))
    health_url = str(runtime.get("health_url", f"{base_url}/")).strip()
    token_env_var = str(runtime.get("token_env_var", "DREAM_API_TOKEN")).strip()
    token_file = str(runtime.get("token_file", "")).strip()

    token = os.getenv(token_env_var)
    if not token and token_file:
        token = _load_exported_token(token_file, token_env_var)

    headers = {"Content-Type": "application/json"}
    if token:
        headers["x-dream-token"] = token

    capsule_payload = {
        "capsule_type": "distill",
        "summary": summary,
        "body": {
            "source_system": payload.get("source_system", "ai_broker"),
            "source_references": payload.get("source_references", []),
            "key_facts": payload.get("key_facts", []),
            "themes": payload.get("themes", ["milestone"]),
            "entities": payload.get("entities", ["AI Broker", "Dream Server"]),
            "relationships": payload.get("relationships", []),
            "open_questions": payload.get("open_questions", []),
            "contradictions": payload.get("contradictions", []),
            "recovery_hints": payload.get("recovery_hints", []),
            "emotional_tone": payload.get("emotional_tone"),
            "stylistic_profile": payload.get("stylistic_profile"),
            "domain_metadata": payload.get("domain_metadata", {}),
        },
        "confidence": float(payload.get("confidence", 0.8)),
        "retention": payload.get("retention", "durable"),
        "scope": payload.get("scope", "private"),
        "trust_level": payload.get("trust_level", "local"),
        "review_status": payload.get("review_status", "unreviewed"),
        "ttl_seconds": payload.get("ttl_seconds"),
        "source_agent": payload.get("source_agent", runtime.get("source_agent", "ai-broker")),
        "source_type": payload.get("source_type", "broker-milestone"),
        "source_ref": payload.get("source_ref"),
        "lineage": payload.get("lineage", {}),
        "metadata": payload.get("metadata", {}),
    }
    if payload.get("parent_capsule_id"):
        capsule_payload["parent_capsule_id"] = payload["parent_capsule_id"]

    try:
        with httpx.Client(timeout=timeout) as client:
            health = client.get(health_url, headers=headers)
            if health.status_code >= 500:
                return {"ok": False, "error": f"Dream server health check failed with HTTP {health.status_code}"}
            response = client.post(f"{base_url}/store_capsule", json=capsule_payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text.strip()
        return {"ok": False, "error": f"Dream REST HTTP {exc.response.status_code}: {detail}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Dream REST connection error: {exc}"}

    return {
        "ok": True,
        "result": {
            "intent": task.get("intent"),
            "capsule_id": data.get("capsule_id"),
            "capsule_uuid": data.get("capsule_uuid"),
            "summary": summary,
        },
        "metadata": {
            "provider_name": "dream_rest",
            "base_url": base_url,
            "health_url": health_url,
        },
    }


# ---------------------------------------------------------------------------
# Anthropic — text LLM
# ---------------------------------------------------------------------------

def _call_anthropic(task: Dict[str, Any]) -> Dict[str, Any]:
    # Delegate vision tasks (intent == "vision" or payload contains image_path)
    # to the dedicated vision adapter.
    payload = task.get("payload") or {}
    if task.get("intent") == "vision" or (isinstance(payload, dict) and "image_path" in payload):
        return _call_anthropic_vision(task)

    try:
        import anthropic as anthropic_sdk
    except ImportError:
        return {"ok": False, "error": "anthropic package not installed. Run: pip install anthropic"}

    intent = task.get("intent", "summarize")
    text = _payload_text(task)

    _SYSTEM_PROMPTS = {
        "summarize": "You are a precise summarization assistant. Produce concise, accurate summaries that capture the key points of the provided text.",
        "analyze": "You are a thorough analytical assistant. Examine the provided content carefully and deliver structured, insightful analysis.",
        "classify": "You are a classification assistant. Categorize the provided content accurately and explain your classification briefly.",
    }
    system = _SYSTEM_PROMPTS.get(
        intent, "You are a helpful AI assistant. Respond clearly and concisely."
    )

    client = anthropic_sdk.Anthropic()

    try:
        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": text}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic_sdk.AuthenticationError:
        return {"ok": False, "error": "Anthropic API key invalid or missing (set ANTHROPIC_API_KEY)"}
    except anthropic_sdk.RateLimitError:
        return {"ok": False, "error": "Anthropic rate limit reached; retry later"}
    except anthropic_sdk.APIStatusError as exc:
        return {"ok": False, "error": f"Anthropic API error {exc.status_code}: {exc.message}"}
    except anthropic_sdk.APIConnectionError:
        return {"ok": False, "error": "Anthropic network error; check connectivity"}

    response_text = next(
        (block.text for block in message.content if block.type == "text"), ""
    )

    return {
        "ok": True,
        "result": {
            "intent": intent,
            "text": response_text,
        },
        "metadata": {
            "model": message.model,
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "cache_read_tokens": getattr(message.usage, "cache_read_input_tokens", 0),
            "cache_creation_tokens": getattr(message.usage, "cache_creation_input_tokens", 0),
            "stop_reason": message.stop_reason,
        },
    }


# ---------------------------------------------------------------------------
# Anthropic — vision
# Preferred path: upload image to Anthropic Files API on first use, store the
# returned file_id in cache/image_files.json keyed by SHA-256 of the image
# bytes.  Subsequent calls with the same image content (regardless of path)
# skip the upload and reference the existing file_id, saving both upload time
# and tokens.  Falls back to inline base64 if the Files API call fails.
# ---------------------------------------------------------------------------

_IMAGE_FILE_CACHE = os.path.join("cache", "image_files.json")

def _call_anthropic_vision(task: Dict[str, Any]) -> Dict[str, Any]:
    try:
        import anthropic as anthropic_sdk
    except ImportError:
        return {"ok": False, "error": "anthropic package not installed. Run: pip install anthropic"}

    payload = task.get("payload") or {}
    image_path = str(payload.get("image_path", ""))
    prompt = str(payload.get("prompt") or "Describe what you see in this image in detail.")

    # Read raw bytes so we can both hash them and encode them.
    try:
        with open(image_path, "rb") as fh:
            image_bytes = fh.read()
    except (OSError, IOError) as exc:
        return {"ok": False, "error": f"Cannot read image file '{image_path}': {exc}"}

    content_hash = hashlib.sha256(image_bytes).hexdigest()
    media_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    if media_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        media_type = "image/jpeg"

    client = anthropic_sdk.Anthropic()

    # System prompt is stable across all vision requests so it caches well.
    system_block = {
        "type": "text",
        "text": "You are a precise visual analysis assistant. Examine images carefully and provide detailed, accurate descriptions and analysis.",
        "cache_control": {"type": "ephemeral"},
    }

    # --- Attempt 1: Anthropic Files API ---
    # Check whether we already uploaded this exact image content.
    file_id = _load_image_file_id(_IMAGE_FILE_CACHE, content_hash)

    if file_id is None:
        # First time we've seen this image — upload it and cache the file_id.
        try:
            filename = os.path.basename(image_path) or "image.jpg"
            uploaded = client.beta.files.upload(
                file=(filename, io.BytesIO(image_bytes), media_type),
            )
            file_id = uploaded.id
            _save_image_file_id(_IMAGE_FILE_CACHE, content_hash, file_id)
        except Exception:
            # Upload failed — file_id stays None and we fall through to base64.
            file_id = None

    message = None

    if file_id:
        try:
            with client.beta.messages.stream(
                model="claude-opus-4-6",
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=[system_block],
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "file", "file_id": file_id}},
                    {"type": "text", "text": prompt},
                ]}],
                betas=["files-api-2025-04-14"],
            ) as stream:
                message = stream.get_final_message()
        except (anthropic_sdk.APIStatusError, anthropic_sdk.APIConnectionError):
            # Files API call failed; fall through to inline base64 below.
            file_id = None

    # --- Attempt 2: Inline base64 fallback ---
    if message is None:
        image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
        try:
            with client.messages.stream(
                model="claude-opus-4-6",
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=[system_block],
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_data,
                    }},
                    {"type": "text", "text": prompt},
                ]}],
            ) as stream:
                message = stream.get_final_message()
        except anthropic_sdk.AuthenticationError:
            return {"ok": False, "error": "Anthropic API key invalid or missing (set ANTHROPIC_API_KEY)"}
        except anthropic_sdk.RateLimitError:
            return {"ok": False, "error": "Anthropic rate limit reached; retry later"}
        except anthropic_sdk.APIStatusError as exc:
            return {"ok": False, "error": f"Anthropic API error {exc.status_code}: {exc.message}"}
        except anthropic_sdk.APIConnectionError:
            return {"ok": False, "error": "Anthropic network error; check connectivity"}

    response_text = next(
        (block.text for block in message.content if block.type == "text"), ""
    )
    return {
        "ok": True,
        "result": {
            "intent": task.get("intent", "vision"),
            "text": response_text,
        },
        "metadata": {
            "model": message.model,
            "image_content_hash": content_hash,
            "image_file_id": file_id,
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "cache_read_tokens": getattr(message.usage, "cache_read_input_tokens", 0),
            "cache_creation_tokens": getattr(message.usage, "cache_creation_input_tokens", 0),
            "stop_reason": message.stop_reason,
        },
    }


# ---------------------------------------------------------------------------
# Google Vision stub
# ---------------------------------------------------------------------------

def _call_google_vision_stub(task: Dict[str, Any]) -> Dict[str, Any]:
    image_path = str(task.get("payload", {}).get("image_path", ""))
    digest = hashlib.sha256(image_path.encode("utf-8")).hexdigest()[:12]
    return {
        "ok": True,
        "result": {
            "provider_note": "Google Vision adapter stub",
            "ocr_text": f"Simulated OCR text for {os.path.basename(image_path)} ({digest})",
        },
        "metadata": {"engine": "google-vision-placeholder"},
    }


# ---------------------------------------------------------------------------
# MCP adapter — JSON-RPC 2.0 over HTTPS
# Calls any MCP server configured in policy["mcp_servers"][server_key].
# The task intent IS the MCP tool name; the task payload maps directly to
# the tool's arguments.  Authentication token is read from the file path
# named in policy (never hardcoded here) and sent as an HTTP header.
# Uses httpx (installed as an anthropic SDK dependency) with a urllib fallback.
# ---------------------------------------------------------------------------

def _call_mcp(
    task: Dict[str, Any],
    policy: Dict[str, Any],
    server_key: str,
) -> Dict[str, Any]:
    mcp_servers = policy.get("mcp_servers", {})
    config = mcp_servers.get(server_key)
    if not config:
        return {"ok": False, "error": f"MCP server '{server_key}' not found in policy['mcp_servers']"}

    url = config.get("url", "")
    if not url:
        return {"ok": False, "error": f"MCP server '{server_key}' has no 'url' configured"}

    auth_header = config.get("auth_header", "x-brain-key")

    # Resolve the access token — try inline value first, then read from file.
    token = config.get("token")
    if not token:
        token_file = config.get("token_file", "")
        if token_file:
            try:
                with open(os.path.expanduser(token_file), "r", encoding="utf-8") as fh:
                    token = fh.read().strip()
            except OSError as exc:
                return {"ok": False, "error": f"Cannot read MCP token file '{token_file}': {exc}"}

    intent = task.get("intent", "")
    payload = task.get("payload") or {}

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers[auth_header] = token

    # MCP JSON-RPC 2.0 tool-call envelope.
    rpc_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": intent,
            "arguments": payload,
        },
    }

    # Attempt call with httpx (present as an anthropic SDK dependency), then
    # fall back to stdlib urllib so there are no hard new requirements.
    data: Dict[str, Any] = {}
    try:
        import httpx
        with httpx.Client(timeout=30.0) as http:
            response = http.post(url, json=rpc_body, headers=headers)
            response.raise_for_status()
            data = decode_mcp_response(response.text)
    except ImportError:
        import urllib.request
        import urllib.error
        body = json.dumps(rpc_body).encode()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = decode_mcp_response(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"MCP HTTP {exc.code}: {exc.reason}"}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": f"MCP connection error: {exc.reason}"}
    except Exception as exc:
        return {"ok": False, "error": f"MCP call failed: {exc}"}

    # Surface any JSON-RPC-level error.
    if "error" in data:
        err = data["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        return {"ok": False, "error": f"MCP error: {msg}"}

    result = data.get("result", {})
    content_blocks = result.get("content", [])
    text_out = "\n".join(
        block.get("text", "")
        for block in content_blocks
        if block.get("type") == "text"
    )

    # MCP signals tool-level errors via isError rather than JSON-RPC error.
    if result.get("isError"):
        return {"ok": False, "error": text_out or "MCP tool returned an error"}

    return {
        "ok": True,
        "result": {
            "intent": intent,
            "tool": intent,
            "text": text_out,
        },
        "metadata": {
            "mcp_server": server_key,
            "mcp_url": url,
        },
    }
