"""AI Auto-Parser: infer a custom protocol template from captured samples.

Give it at least three raw messages from an unknown device; it asks an LLM to
work out the structure and returns a config that ``CustomProtocol`` accepts.
The result is validated locally before it is returned. No third-party SDK is
needed - only the standard library.

Providers
---------
``anthropic``  Anthropic Messages API
``openai``     any OpenAI-compatible ``/chat/completions`` endpoint (OpenAI,
               Azure, Ollama, vLLM, LM Studio ...) via ``base_url``

Privacy: the samples are sent to the provider you choose. Remove real IMEIs
or coordinates first if that matters to you, or point ``base_url`` at a
model you host yourself.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from gps_tester.core.checksums import ALGORITHMS
from gps_tester.protocols.custom import FIELD_HELP, CustomProtocol

DEFAULT_MODELS = {"anthropic": "claude-sonnet-4-5", "openai": "gpt-4o-mini"}
DEFAULT_BASE_URLS = {"anthropic": "https://api.anthropic.com/v1", "openai": "https://api.openai.com/v1"}

SYSTEM_PROMPT = "You are an expert in GPS tracker and IoT wire protocols. Reply with a single JSON object only."

PROMPT = """Below are {count} messages captured from an unknown GPS tracking device.
Infer the protocol structure and produce a template that can generate new,
valid messages of the same kind with different values.

Return ONE JSON object with this shape (no markdown, no commentary):

{{
  "name": "<short-name>",
  "transport": "tcp" | "udp",
  "encoding": "text" | "hex",
  "checksum": {{"algorithm": "none" | {algorithms}, "format": "<python format, e.g. {{:02X}}>", "skip": <int>}},
  "messages": {{
    "login":     {{"template": "...", "expect_response": true|false}},   // omit if the samples show no login
    "location":  {{"template": "...", "expect_response": true|false}},   // required
    "heartbeat": {{"template": "...", "expect_response": true|false}}    // omit if unknown
  }},
  "notes": "<what each field means and anything you were unsure about>"
}}

Template rules:
- Templates are Python str.format strings. Literal braces must be doubled.
- With "encoding": "text" the rendered string is sent as UTF-8 (write \\r\\n explicitly).
- With "encoding": "hex" the template must render to hex digits, e.g. "7878{{seq:04x}}{{lat_e7:08x}}".
- "{{checksum}}" is replaced by the checksum of everything rendered before it,
  skipping the first `skip` bytes (e.g. 1 to skip a leading "$").
- Keep constant parts of the samples literal. Use ONLY these placeholders:
{fields}

Samples:
{samples}
"""


def _describe(sample: str) -> str:
    if sample.lower().startswith("hex:"):
        raw = bytes.fromhex(re.sub(r"\s+", "", sample[4:]))
        return f"hex: {raw.hex()}\n   ascii: {raw.decode('latin-1')!r}"
    return f"text: {sample!r}\n   hex: {sample.encode().hex()}"


def build_prompt(samples: List[str]) -> str:
    fields = "\n".join(f"  {{{name}}} - {text}" for name, text in FIELD_HELP.items())
    listing = "\n".join(f"{i}. {_describe(s)}" for i, s in enumerate(samples, 1))
    algorithms = " | ".join(f'"{name}"' for name in sorted(ALGORITHMS))
    return PROMPT.format(count=len(samples), fields=fields, samples=listing, algorithms=algorithms)


def _post(url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"LLM API returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the LLM API: {exc.reason}") from exc


def call_llm(prompt: str, provider: str, model: str, api_key: str, base_url: str,
             timeout: float = 120.0) -> str:
    base_url = base_url.rstrip("/")
    if provider == "anthropic":
        data = _post(f"{base_url}/messages",
                     {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                     {"model": model, "max_tokens": 4000, "system": SYSTEM_PROMPT,
                      "messages": [{"role": "user", "content": prompt}]}, timeout)
        return "".join(block.get("text", "") for block in data.get("content", []))
    if provider == "openai":
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        data = _post(f"{base_url}/chat/completions", headers,
                     {"model": model, "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                                   {"role": "user", "content": prompt}]}, timeout)
        return data["choices"][0]["message"]["content"]
    raise ValueError(f"Unknown LLM provider {provider!r} (use 'anthropic' or 'openai')")


def extract_json(text: str) -> Dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The model did not return a JSON object")
    return json.loads(text[start:end + 1])


def infer_template(samples: List[str], provider: str = "anthropic", model: Optional[str] = None,
                   api_key: Optional[str] = None, base_url: Optional[str] = None) -> Dict[str, Any]:
    """Return a validated custom-protocol config inferred from ``samples``."""
    if len(samples) < 3:
        raise ValueError("Provide at least 3 sample messages")
    provider = provider.lower()
    api_key = (api_key or os.environ.get("GPS_TESTER_LLM_API_KEY")
               or os.environ.get("ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY") or "")
    base_url = base_url or os.environ.get("GPS_TESTER_LLM_BASE_URL") or DEFAULT_BASE_URLS.get(provider, "")
    if not api_key and base_url == DEFAULT_BASE_URLS.get(provider):
        raise ValueError("No API key. Set GPS_TESTER_LLM_API_KEY or pass --llm-api-key")
    model = model or os.environ.get("GPS_TESTER_LLM_MODEL") or DEFAULT_MODELS.get(provider, "")

    config = extract_json(call_llm(build_prompt(samples), provider, model, api_key, base_url))
    CustomProtocol(config)   # raises ValueError if the model produced an unusable template
    return config
