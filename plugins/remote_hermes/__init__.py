"""Remote Hermes node tools.

This plugin lets a coordinator Hermes call another device's Hermes API
server. Node credentials are read from ~/.hermes/remote-hermes-nodes.env or
REMOTE_HERMES_NODES_FILE. Secrets are never returned in tool output.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

from tools.registry import tool_error, tool_result

DEFAULT_NODES_FILE = Path.home() / ".hermes" / "remote-hermes-nodes.env"


def _nodes_file() -> Path:
    return Path(os.environ.get("REMOTE_HERMES_NODES_FILE") or DEFAULT_NODES_FILE)


def _parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _load_nodes() -> Dict[str, Dict[str, str]]:
    data = _parse_env_file(_nodes_file())
    aliases = set()
    for key in data:
        match = re.match(r"HERMES_NODE_([A-Z0-9_]+)_(NAME|API_BASE|API_KEY)$", key)
        if match:
            aliases.add(match.group(1))

    nodes: Dict[str, Dict[str, str]] = {}
    for alias in aliases:
        name = data.get(f"HERMES_NODE_{alias}_NAME") or alias
        base = (data.get(f"HERMES_NODE_{alias}_API_BASE") or "").rstrip("/")
        key = data.get(f"HERMES_NODE_{alias}_API_KEY") or ""
        if base and key:
            node = {"name": name, "base": base, "key": key, "alias": alias}
            nodes[name.lower()] = node
            nodes[alias.lower()] = node
    return nodes


def _node_or_error(name: str | None) -> Dict[str, str] | str:
    nodes = _load_nodes()
    if not nodes:
        return tool_error(f"No remote Hermes nodes configured in {_nodes_file()}.")
    if not name:
        unique = {value["name"].lower(): value for value in nodes.values()}
        if len(unique) == 1:
            return next(iter(unique.values()))
        return tool_error("Multiple remote Hermes nodes exist; specify node name.")
    key = str(name).strip().lower().lstrip("@")
    node = nodes.get(key)
    if not node:
        names = sorted({value["name"] for value in nodes.values()})
        return tool_error(f"Unknown remote Hermes node '{name}'. Available: {', '.join(names)}")
    return node


def _http_json(
    method: str,
    url: str,
    *,
    api_key: str | None = None,
    body: Any = None,
    timeout: int = 60,
    extra_headers: Dict[str, str] | None = None,
) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(f"HTTP {exc.code}: {raw}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"connection failed: {exc.reason}") from exc


def _extract_text(response: Any) -> str:
    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            message = choice.get("message") if isinstance(choice, dict) else None
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]

        # OpenAI Responses API shape: output is a list of items; assistant
        # message content contains output_text parts.
        output = response.get("output")
        if isinstance(output, str):
            return output
        if isinstance(output, list):
            texts = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            texts.append(part["text"])
            if texts:
                return "\n".join(texts)
    return json.dumps(response, ensure_ascii=False)[:4000]


def _remote_hermes_list(args: dict, **kw) -> str:
    nodes = _load_nodes()
    unique = {value["name"].lower(): value for value in nodes.values()}
    return tool_result({
        "success": True,
        "nodes": [
            {
                "name": node["name"],
                "alias": node["alias"],
                "api_base": node["base"],
                "has_api_key": bool(node.get("key")),
            }
            for node in unique.values()
        ],
    })


def _remote_hermes_health(args: dict, **kw) -> str:
    node = _node_or_error(args.get("node"))
    if isinstance(node, str):
        return node
    try:
        payload = _http_json("GET", f"{node['base']}/health", timeout=int(args.get("timeout_seconds") or 8))
        return tool_result({"success": True, "node": node["name"], "health": payload})
    except Exception as exc:
        return tool_error(f"Remote Hermes health check failed for {node['name']}: {exc}")


def _remote_hermes_run(args: dict, **kw) -> str:
    node = _node_or_error(args.get("node"))
    if isinstance(node, str):
        return node
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return tool_error("prompt is required")

    timeout = int(args.get("timeout_seconds") or 180)
    session_key = str(args.get("session_key") or f"vps-remote-{node['name'].lower()}")[:128]
    body = {
        "model": "hermes-agent",
        "input": prompt,
        "instructions": (
            "You are being called remotely by the user's VPS Hermes coordinator. "
            "Execute on this local device when appropriate. Be concise in the final result."
        ),
        # Responses API named conversations auto-chain to the latest response
        # for the same key, giving remote calls stable session continuity.
        "conversation": session_key,
        "store": True,
    }
    try:
        payload = _http_json(
            "POST",
            f"{node['base']}/v1/responses",
            api_key=node["key"],
            body=body,
            timeout=timeout,
            extra_headers={"X-Hermes-Session-Key": session_key},
        )
        return tool_result({
            "success": True,
            "node": node["name"],
            "session_key": session_key,
            "content": _extract_text(payload),
            "usage": payload.get("usage") if isinstance(payload, dict) else None,
        })
    except Exception as exc:
        return tool_error(f"Remote Hermes run failed for {node['name']}: {exc}")


REMOTE_HERMES_LIST_SCHEMA = {
    "name": "remote_hermes_list",
    "description": (
        "List configured remote Hermes nodes. Use this before routing work to another device. "
        "Does not reveal API keys."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

REMOTE_HERMES_HEALTH_SCHEMA = {
    "name": "remote_hermes_health",
    "description": "Check whether a remote Hermes node is reachable through its API server.",
    "parameters": {
        "type": "object",
        "properties": {
            "node": {
                "type": "string",
                "description": "Node name or alias, e.g. MBP, PC_AU, PC_CN. Optional when only one node is configured.",
            },
            "timeout_seconds": {"type": "integer", "description": "HTTP timeout in seconds.", "default": 8},
        },
        "additionalProperties": False,
    },
}

REMOTE_HERMES_RUN_SCHEMA = {
    "name": "remote_hermes_run",
    "description": (
        "Send a user task to a specific remote Hermes node so it executes on that device, not on the VPS. "
        "Use for requests like '@MBP create a file on Desktop' or 'on PC_CN restart Minecraft'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "Target node name or alias, e.g. MBP, PC_AU, PC_CN."},
            "prompt": {
                "type": "string",
                "description": "The exact task to run on the remote device. Include target device and relevant paths.",
            },
            "session_key": {"type": "string", "description": "Optional stable session/memory key for the remote Hermes run."},
            "timeout_seconds": {"type": "integer", "description": "HTTP timeout in seconds for this run.", "default": 180},
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}


def _check_remote_hermes_available() -> bool:
    return bool(_load_nodes())


def register(ctx) -> None:
    ctx.register_tool(
        name="remote_hermes_list",
        toolset="remote_hermes",
        schema=REMOTE_HERMES_LIST_SCHEMA,
        handler=_remote_hermes_list,
        check_fn=_check_remote_hermes_available,
        emoji="desktop",
    )
    ctx.register_tool(
        name="remote_hermes_health",
        toolset="remote_hermes",
        schema=REMOTE_HERMES_HEALTH_SCHEMA,
        handler=_remote_hermes_health,
        check_fn=_check_remote_hermes_available,
        emoji="stethoscope",
    )
    ctx.register_tool(
        name="remote_hermes_run",
        toolset="remote_hermes",
        schema=REMOTE_HERMES_RUN_SCHEMA,
        handler=_remote_hermes_run,
        check_fn=_check_remote_hermes_available,
        emoji="satellite",
    )
