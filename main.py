#!/usr/bin/env python3
"""docat - Simple read-only CLI wrapper for the Docker Engine API (Unix & TCP sockets)."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import ssl
import struct
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any


DEFAULT_HOST = "unix:///var/run/docker.sock"
DEFAULT_TIMEOUT = 10.0


class DocatError(Exception):
    """Base error for docat failures."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection over a Unix domain socket."""

    def __init__(self, socket_path: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def parse_host(
    host: str,
    timeout: float = DEFAULT_TIMEOUT,
    cert_path: str | None = None,
    tls_verify: bool = True,
) -> http.client.HTTPConnection:
    """Create an HTTPConnection for a Unix or TCP Docker socket."""
    host = host.strip()
    if not host:
        host = DEFAULT_HOST

    if host.startswith("unix://"):
        socket_path = host[len("unix://") :]
        return UnixHTTPConnection(socket_path, timeout=timeout)

    if host.startswith("/") or host.startswith("./") or host.startswith("../"):
        return UnixHTTPConnection(host, timeout=timeout)

    if "://" not in host:
        host = f"tcp://{host}"

    parsed = urllib.parse.urlparse(host)
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname or "localhost"

    if scheme in ("tcp", "http"):
        port = parsed.port or (2376 if cert_path else 2375)
        if cert_path:
            return _create_https_connection(hostname, port, timeout, cert_path, tls_verify)
        return http.client.HTTPConnection(hostname, port, timeout=timeout)

    if scheme == "https":
        port = parsed.port or 2376
        return _create_https_connection(hostname, port, timeout, cert_path, tls_verify)

    raise DocatError(f"Unsupported host scheme '{scheme}' in '{host}'. Use unix://, tcp://, http://, or https://.")


def _create_https_connection(
    hostname: str,
    port: int,
    timeout: float,
    cert_path: str | None,
    tls_verify: bool,
) -> http.client.HTTPSConnection:
    if tls_verify:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl._create_unverified_context()

    if cert_path:
        ca_file = os.path.join(cert_path, "ca.pem")
        cert_file = os.path.join(cert_path, "cert.pem")
        key_file = os.path.join(cert_path, "key.pem")
        if tls_verify and os.path.exists(ca_file):
            ctx.load_verify_locations(cafile=ca_file)
        if os.path.exists(cert_file) and os.path.exists(key_file):
            ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)

    return http.client.HTTPSConnection(hostname, port, timeout=timeout, context=ctx)


class DockerClient:
    """Minimal read-only Docker Engine API client."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        timeout: float = DEFAULT_TIMEOUT,
        api_version: str | None = None,
        cert_path: str | None = None,
        tls_verify: bool = True,
    ) -> None:
        self.host = host
        self.timeout = timeout
        self.api_version = api_version.strip().lstrip("v") if api_version else None
        self.cert_path = cert_path
        self.tls_verify = tls_verify

    def _build_path(self, path: str, query: dict[str, Any] | None = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        if self.api_version and not re.match(r"^/v\d+\.\d+/", path):
            path = f"/v{self.api_version}{path}"
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                qs = urllib.parse.urlencode(clean, doseq=True)
                sep = "&" if "?" in path else "?"
                path = f"{path}{sep}{qs}"
        return path

    def request_raw(self, path: str, query: dict[str, Any] | None = None) -> tuple[int, dict[str, str], bytes]:
        full_path = self._build_path(path, query)
        conn = parse_host(self.host, timeout=self.timeout, cert_path=self.cert_path, tls_verify=self.tls_verify)
        try:
            conn.request("GET", full_path, headers={"Host": "localhost", "User-Agent": "docat/0.1.0"})
            resp = conn.getresponse()
            body = resp.read()
            headers = {k.lower(): v for k, v in resp.getheaders()}
            status = resp.status
        except FileNotFoundError as exc:
            raise DocatError(f"Docker socket not found ({self.host}): {exc}") from exc
        except PermissionError as exc:
            raise DocatError(f"Permission denied connecting to Docker socket ({self.host}): {exc}") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise DocatError(f"Failed to connect to Docker ({self.host}): {exc}") from exc
        finally:
            conn.close()

        if status >= 400:
            err_msg = body.decode("utf-8", errors="replace").strip()
            try:
                parsed_err = json.loads(err_msg)
                if isinstance(parsed_err, dict) and "message" in parsed_err:
                    err_msg = parsed_err["message"]
            except ValueError:
                pass
            raise DocatError(f"HTTP {status}: {err_msg or 'Request failed'}", status=status)

        return status, headers, body

    def get_json(self, path: str, query: dict[str, Any] | None = None) -> Any:
        _, _, body = self.request_raw(path, query)
        if not body:
            return None
        text = body.decode("utf-8", errors="replace").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Handle newline-delimited JSON (e.g., /events)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if lines:
                try:
                    return [json.loads(line) for line in lines]
                except json.JSONDecodeError:
                    pass
            return text


def demux_docker_stream(data: bytes) -> str:
    """Decode Docker's multiplexed stdout/stderr stream or plain text logs."""
    if not data:
        return ""
    out: list[bytes] = []
    pos = 0
    length = len(data)
    while pos + 8 <= length:
        stream_type, b1, b2, b3, frame_size = struct.unpack(">BBBBi", data[pos : pos + 8])
        if b1 != 0 or b2 != 0 or b3 != 0 or stream_type not in (0, 1, 2, 3) or frame_size < 0:
            # Not a valid multiplexed header; treat the rest as raw TTY output
            return data.decode("utf-8", errors="replace")
        pos += 8
        if pos + frame_size > length:
            out.append(data[pos:])
            break
        out.append(data[pos : pos + frame_size])
        pos += frame_size
    if not out and length > 0:
        return data.decode("utf-8", errors="replace")
    return b"".join(out).decode("utf-8", errors="replace")


# --- YAML / JSON / Table Formatting ---

_YAML_SPECIAL_STRINGS = {
    "true",
    "false",
    "null",
    "~",
    "yes",
    "no",
    "on",
    "off",
    "y",
    "n",
    ".inf",
    "-.inf",
    ".nan",
}
_YAML_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]*$")


def _yaml_scalar(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val)
    if s == "":
        return '""'
    if (
        s.lower() in _YAML_SPECIAL_STRINGS
        or s[0] in "-?:,[]{}#&*!|>'\"%@` \t"
        or s[-1] in " \t:"
        or any(ch in s for ch in (": ", " #", "\n", "\r", "\t", "\x00"))
    ):
        return json.dumps(s, ensure_ascii=False)
    try:
        float(s)
        return json.dumps(s, ensure_ascii=False)
    except ValueError:
        return s


def _yaml_key(key: Any) -> str:
    s = str(key)
    if not s or s.lower() in _YAML_SPECIAL_STRINGS or not _YAML_SAFE_KEY_RE.match(s):
        return json.dumps(s, ensure_ascii=False)
    return s


def _to_yaml_lines(obj: Any, indent: int = 0) -> list[str]:
    prefix = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return [f"{prefix}{{}}"]
        lines: list[str] = []
        for k, v in obj.items():
            key_str = _yaml_key(k)
            if isinstance(v, dict):
                if not v:
                    lines.append(f"{prefix}{key_str}: {{}}")
                else:
                    lines.append(f"{prefix}{key_str}:")
                    lines.extend(_to_yaml_lines(v, indent + 1))
            elif isinstance(v, list):
                if not v:
                    lines.append(f"{prefix}{key_str}: []")
                else:
                    lines.append(f"{prefix}{key_str}:")
                    lines.extend(_to_yaml_lines(v, indent + 1))
            elif isinstance(v, str) and "\n" in v:
                lines.append(f"{prefix}{key_str}: |-")
                for subline in v.splitlines():
                    lines.append(f"{prefix}  {subline}")
            else:
                lines.append(f"{prefix}{key_str}: {_yaml_scalar(v)}")
        return lines

    if isinstance(obj, list):
        if not obj:
            return [f"{prefix}[]"]
        lines = []
        for item in obj:
            if isinstance(item, dict):
                if not item:
                    lines.append(f"{prefix}- {{}}")
                else:
                    sub_lines = _to_yaml_lines(item, indent + 1)
                    first = sub_lines[0].lstrip()
                    lines.append(f"{prefix}- {first}")
                    lines.extend(sub_lines[1:])
            elif isinstance(item, list):
                if not item:
                    lines.append(f"{prefix}- []")
                else:
                    lines.append(f"{prefix}-")
                    lines.extend(_to_yaml_lines(item, indent + 1))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
        return lines

    return [f"{prefix}{_yaml_scalar(obj)}"]


def dump_yaml(data: Any) -> str:
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip("\n")
    except ImportError:
        return "\n".join(_to_yaml_lines(data))


def format_bytes(size: int | float | None) -> str:
    if size is None or size < 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    val = float(size)
    for unit in units:
        if abs(val) < 1000.0 or unit == units[-1]:
            return f"{int(val)}{unit}" if unit == "B" else f"{val:.1f}{unit}"
        val /= 1000.0
    return f"{val:.1f}TB"


def format_timestamp(ts: int | float | str | None) -> str:
    if ts is None or ts == "":
        return "-"
    if isinstance(ts, (int, float)):
        if ts <= 0:
            return "-"
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    s = str(ts)
    # Trim nanoseconds in ISO timestamps like 2025-01-01T12:00:00.123456789Z
    if "T" in s and len(s) >= 19:
        return s[:19].replace("T", " ")
    return s


def render_table(headers: list[str], rows: list[list[Any]]) -> str:
    str_rows = [[str(cell if cell is not None else "-") for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            if i == len(cells) - 1:
                parts.append(cell)
            else:
                parts.append(cell.ljust(widths[i]))
        return "  ".join(parts)

    lines = [fmt_row(headers)]
    for row in str_rows:
        lines.append(fmt_row(row))
    return "\n".join(lines)


def short_id(val: str | None, length: int = 12) -> str:
    if not val:
        return "-"
    if val.startswith("sha256:"):
        val = val[len("sha256:") :]
    return val[:length]


# --- Table Summarizers per Resource ---


def summarize_containers(items: list[dict[str, Any]]) -> str:
    headers = ["CONTAINER ID", "NAMES", "IMAGE", "STATE", "STATUS", "PORTS"]
    rows = []
    for c in items:
        names = [n.lstrip("/") for n in (c.get("Names") or [])]
        ports_list = []
        for p in c.get("Ports") or []:
            priv = p.get("PrivatePort")
            pub = p.get("PublicPort")
            typ = p.get("Type", "tcp")
            ip = p.get("IP", "")
            if pub:
                prefix = f"{ip}:{pub}->" if ip else f"{pub}->"
                ports_list.append(f"{prefix}{priv}/{typ}")
            elif priv:
                ports_list.append(f"{priv}/{typ}")
        rows.append(
            [
                short_id(c.get("Id")),
                ",".join(names) or "-",
                c.get("Image") or "-",
                c.get("State") or "-",
                c.get("Status") or "-",
                ", ".join(ports_list) or "-",
            ]
        )
    return render_table(headers, rows)


def summarize_images(items: list[dict[str, Any]]) -> str:
    headers = ["IMAGE ID", "REPO TAGS", "CREATED", "SIZE"]
    rows = []
    for img in items:
        tags = img.get("RepoTags") or ["<none>:<none>"]
        rows.append(
            [
                short_id(img.get("Id")),
                ", ".join(tags),
                format_timestamp(img.get("Created")),
                format_bytes(img.get("Size")),
            ]
        )
    return render_table(headers, rows)


def summarize_networks(items: list[dict[str, Any]]) -> str:
    headers = ["NETWORK ID", "NAME", "DRIVER", "SCOPE", "INTERNAL"]
    rows = []
    for net in items:
        rows.append(
            [
                short_id(net.get("Id")),
                net.get("Name") or "-",
                net.get("Driver") or "-",
                net.get("Scope") or "-",
                str(bool(net.get("Internal", False))).lower(),
            ]
        )
    return render_table(headers, rows)


def summarize_volumes(data: dict[str, Any] | list[dict[str, Any]]) -> str:
    items = data.get("Volumes") or [] if isinstance(data, dict) else data
    headers = ["VOLUME NAME", "DRIVER", "SCOPE", "MOUNTPOINT"]
    rows = []
    for v in items:
        rows.append(
            [
                v.get("Name") or "-",
                v.get("Driver") or "-",
                v.get("Scope") or "-",
                v.get("Mountpoint") or "-",
            ]
        )
    return render_table(headers, rows)


def summarize_nodes(items: list[dict[str, Any]]) -> str:
    headers = ["ID", "HOSTNAME", "STATUS", "AVAILABILITY", "ROLE", "ENGINE"]
    rows = []
    for n in items:
        spec = n.get("Spec") or {}
        desc = n.get("Description") or {}
        status = n.get("Status") or {}
        engine = (desc.get("Engine") or {}).get("EngineVersion") or "-"
        mgr = n.get("ManagerStatus") or {}
        role = spec.get("Role") or "-"
        if mgr.get("Leader"):
            role = f"{role} (leader)"
        rows.append(
            [
                short_id(n.get("ID")),
                desc.get("Hostname") or "-",
                status.get("State") or "-",
                spec.get("Availability") or "-",
                role,
                engine,
            ]
        )
    return render_table(headers, rows)


def summarize_services(items: list[dict[str, Any]]) -> str:
    headers = ["ID", "NAME", "MODE", "IMAGE", "PORTS"]
    rows = []
    for s in items:
        spec = s.get("Spec") or {}
        mode_obj = spec.get("Mode") or {}
        if "Replicated" in mode_obj:
            reps = mode_obj["Replicated"].get("Replicas", 0)
            mode = f"replicated ({reps})"
        elif "Global" in mode_obj:
            mode = "global"
        else:
            mode = "-"
        task_tmpl = spec.get("TaskTemplate") or {}
        container_spec = task_tmpl.get("ContainerSpec") or {}
        image = container_spec.get("Image") or "-"
        if "@" in image:
            image = image.split("@", 1)[0]
        ports_list = []
        endpoint = s.get("Endpoint") or {}
        for p in endpoint.get("Ports") or []:
            pub = p.get("PublishedPort")
            tgt = p.get("TargetPort")
            proto = p.get("Protocol", "tcp")
            if pub:
                ports_list.append(f"*:{pub}->{tgt}/{proto}")
            elif tgt:
                ports_list.append(f"{tgt}/{proto}")
        rows.append(
            [
                short_id(s.get("ID")),
                spec.get("Name") or "-",
                mode,
                image,
                ", ".join(ports_list) or "-",
            ]
        )
    return render_table(headers, rows)


def summarize_tasks(items: list[dict[str, Any]]) -> str:
    headers = ["ID", "SERVICE ID", "NODE ID", "SLOT", "DESIRED STATE", "CURRENT STATE", "IMAGE", "ERROR"]
    rows = []
    for t in items:
        spec = t.get("Spec") or {}
        container_spec = spec.get("ContainerSpec") or {}
        image = container_spec.get("Image") or "-"
        if "@" in image:
            image = image.split("@", 1)[0]
        status = t.get("Status") or {}
        state = status.get("State") or "-"
        ts = format_timestamp(status.get("Timestamp"))
        cur_state = f"{state} ({ts})" if ts != "-" else state
        rows.append(
            [
                short_id(t.get("ID")),
                short_id(t.get("ServiceID")),
                short_id(t.get("NodeID")),
                t.get("Slot") if t.get("Slot") is not None else "-",
                t.get("DesiredState") or "-",
                cur_state,
                image,
                status.get("Err") or "",
            ]
        )
    return render_table(headers, rows)


def summarize_configs_or_secrets(items: list[dict[str, Any]]) -> str:
    headers = ["ID", "NAME", "CREATED", "UPDATED"]
    rows = []
    for item in items:
        spec = item.get("Spec") or {}
        rows.append(
            [
                short_id(item.get("ID")),
                spec.get("Name") or "-",
                format_timestamp(item.get("CreatedAt")),
                format_timestamp(item.get("UpdatedAt")),
            ]
        )
    return render_table(headers, rows)


def summarize_plugins(items: list[dict[str, Any]]) -> str:
    headers = ["ID", "NAME", "ENABLED", "DESCRIPTION"]
    rows = []
    for p in items:
        cfg = p.get("Config") or {}
        rows.append(
            [
                short_id(p.get("Id")),
                p.get("Name") or "-",
                str(bool(p.get("Enabled"))).lower(),
                cfg.get("Description") or "-",
            ]
        )
    return render_table(headers, rows)


def summarize_top(data: dict[str, Any]) -> str:
    titles = data.get("Titles") or []
    procs = data.get("Processes") or []
    if not titles:
        return dump_yaml(data)
    return render_table(titles, procs)


def summarize_history(items: list[dict[str, Any]]) -> str:
    headers = ["IMAGE", "CREATED", "SIZE", "COMMENT", "CREATED BY"]
    rows = []
    for h in items:
        created_by = (h.get("CreatedBy") or "").replace("\n", " ")
        if len(created_by) > 60:
            created_by = created_by[:57] + "..."
        rows.append(
            [
                short_id(h.get("Id")),
                format_timestamp(h.get("Created")),
                format_bytes(h.get("Size")),
                h.get("Comment") or "-",
                created_by or "-",
            ]
        )
    return render_table(headers, rows)


def summarize_changes(items: list[dict[str, Any]]) -> str:
    kind_map: dict[Any, str] = {0: "Modified (C)", 1: "Added (A)", 2: "Deleted (D)"}
    headers = ["KIND", "PATH"]
    rows = [[kind_map.get(item.get("Kind"), str(item.get("Kind"))), item.get("Path") or "-"] for item in items]
    return render_table(headers, rows)


def summarize_df(data: dict[str, Any]) -> str:
    images = data.get("Images") or []
    containers = data.get("Containers") or []
    volumes = data.get("Volumes") or []
    build_cache = data.get("BuildCache") or []

    img_size = sum(i.get("Size", 0) for i in images)
    cont_size = sum(c.get("SizeRw", 0) for c in containers)
    vol_size = sum((v.get("UsageData") or {}).get("Size", 0) for v in volumes if (v.get("UsageData") or {}).get("Size", -1) >= 0)
    cache_size = sum(b.get("Size", 0) for b in build_cache)

    headers = ["TYPE", "TOTAL", "SIZE"]
    rows = [
        ["Images", len(images), format_bytes(img_size)],
        ["Containers", len(containers), format_bytes(cont_size)],
        ["Local Volumes", len(volumes), format_bytes(vol_size)],
        ["Build Cache", len(build_cache), format_bytes(cache_size)],
    ]
    return render_table(headers, rows)


def format_output(
    data: Any,
    output_format: str,
    compact: bool = False,
    table_fn: Any = None,
) -> str:
    fmt = output_format.lower()
    if fmt == "json":
        if compact:
            return json.dumps(data, ensure_ascii=False)
        return json.dumps(data, indent=2, ensure_ascii=False)
    if fmt in ("yaml", "yml"):
        return dump_yaml(data)
    # "table" or "auto"
    if table_fn is not None and data is not None:
        return table_fn(data)
    if isinstance(data, str):
        return data
    if fmt == "table" and isinstance(data, dict):
        # Simple key-value table for flat/top-level dictionary when -o table is explicitly requested
        rows = []
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                v_str = json.dumps(v, ensure_ascii=False)
                if len(v_str) > 80:
                    v_str = v_str[:77] + "..."
            else:
                v_str = str(v)
            rows.append([str(k), v_str])
        return render_table(["KEY", "VALUE"], rows)
    return json.dumps(data, indent=2, ensure_ascii=False)


def parse_filters(filter_args: list[str] | None) -> str | None:
    """Convert repeatable -f key=value arguments into Docker's JSON-encoded filters map."""
    if not filter_args:
        return None
    filters: dict[str, list[str]] = {}
    for item in filter_args:
        if "=" not in item:
            raise DocatError(f"Invalid filter '{item}': expected format key=value")
        k, v = item.split("=", 1)
        filters.setdefault(k.strip(), []).append(v.strip())
    return json.dumps(filters)


# --- Resource Routing ---

RESOURCE_ALIASES: dict[str, str] = {
    "container": "containers",
    "containers": "containers",
    "c": "containers",
    "ps": "containers",
    "image": "images",
    "images": "images",
    "img": "images",
    "i": "images",
    "network": "networks",
    "networks": "networks",
    "net": "networks",
    "n": "networks",
    "volume": "volumes",
    "volumes": "volumes",
    "vol": "volumes",
    "v": "volumes",
    "swarm": "swarm",
    "node": "nodes",
    "nodes": "nodes",
    "service": "services",
    "services": "services",
    "svc": "services",
    "task": "tasks",
    "tasks": "tasks",
    "config": "configs",
    "configs": "configs",
    "secret": "secrets",
    "secrets": "secrets",
    "plugin": "plugins",
    "plugins": "plugins",
    "info": "info",
    "version": "version",
    "ping": "ping",
    "df": "df",
    "events": "events",
    "system": "system",
    "inspect": "inspect",
    "logs": "logs",
    "raw": "raw",
    "get": "raw",
}

LIST_ACTIONS = {"list", "ls", "ps"}
INSPECT_ACTIONS = {"inspect", "show", "get", "info"}


def execute_command(client: DockerClient, args: argparse.Namespace) -> str:
    tokens = list(args.command)
    if not tokens:
        raise DocatError("No command specified. Run 'docat --help' for usage.")

    first = tokens[0].lower()
    resource = RESOURCE_ALIASES.get(first)
    if not resource:
        raise DocatError(
            f"Unknown resource or command '{tokens[0]}'. "
            "Supported: containers, images, networks, volumes, swarm, nodes, services, tasks, "
            "configs, secrets, plugins, info, version, ping, df, events, raw."
        )

    rest = tokens[1:]
    filters_json = parse_filters(args.filter)
    out_fmt = args.output
    compact = args.compact

    # Top-level shortcuts:
    # `docat inspect <id>` -> tries container, image, network, volume, service, node, task
    if resource == "inspect":
        if not rest:
            raise DocatError("Usage: docat inspect <id-or-name>")
        target = rest[0]
        return _inspect_any(client, target, out_fmt, compact)

    if resource == "logs":
        if not rest:
            raise DocatError("Usage: docat logs <container-id-or-name>")
        return _container_logs(client, rest[0], args)

    if resource == "raw":
        if not rest:
            raise DocatError("Usage: docat raw </endpoint/path>")
        data = client.get_json(rest[0])
        return format_output(data, out_fmt if out_fmt != "auto" else "json", compact=compact)

    if resource == "system":
        sub = rest[0].lower() if rest else "info"
        if sub == "df":
            resource = "df"
        elif sub == "events":
            resource = "events"
        elif sub == "version":
            resource = "version"
        elif sub == "ping":
            resource = "ping"
        else:
            resource = "info"
        rest = rest[1:]

    if resource == "info":
        data = client.get_json("/info")
        return format_output(data, out_fmt, compact=compact)

    if resource == "version":
        data = client.get_json("/version")
        return format_output(data, out_fmt, compact=compact)

    if resource == "ping":
        _, _, body = client.request_raw("/_ping")
        text = body.decode("utf-8", errors="replace").strip() or "OK"
        if out_fmt == "json":
            return format_output({"status": text}, "json", compact=compact)
        if out_fmt in ("yaml", "yml"):
            return format_output({"status": text}, "yaml")
        return text

    if resource == "df":
        data = client.get_json("/system/df")
        return format_output(data, out_fmt, compact=compact, table_fn=summarize_df)

    if resource == "events":
        now = int(time.time())
        since = args.since if args.since is not None else max(0, now - 3600)
        until = args.until if args.until is not None else now
        query: dict[str, Any] | None = {"since": since, "until": until}
        if filters_json:
            query["filters"] = filters_json
        data = client.get_json("/events", query=query)
        return format_output(data if data is not None else [], out_fmt if out_fmt != "auto" else "json", compact=compact)

    if resource == "swarm":
        action = rest[0].lower() if rest else "inspect"
        if action not in INSPECT_ACTIONS and action not in LIST_ACTIONS:
            raise DocatError(f"Unsupported swarm action '{action}'. Use 'docat swarm [inspect]'.")
        data = client.get_json("/swarm")
        return format_output(data, out_fmt, compact=compact)

    # Determine action & target for standard resources
    action, target = _resolve_action_and_target(rest)

    if resource == "containers":
        if action in LIST_ACTIONS:
            query = {}
            if args.all:
                query["all"] = "1"
            if args.limit is not None:
                query["limit"] = str(args.limit)
            if args.size:
                query["size"] = "1"
            if filters_json:
                query["filters"] = filters_json
            data = client.get_json("/containers/json", query=query)
            return format_output(data, out_fmt, compact=compact, table_fn=summarize_containers)

        if not target:
            raise DocatError(f"Container ID or name required for '{action}'.")

        if action in INSPECT_ACTIONS:
            query = {"size": "1"} if args.size else None
            data = client.get_json(f"/containers/{urllib.parse.quote(target, safe='')}/json", query=query)
            return format_output(data, out_fmt, compact=compact)

        if action == "logs":
            return _container_logs(client, target, args)

        if action == "top":
            data = client.get_json(f"/containers/{urllib.parse.quote(target, safe='')}/top")
            return format_output(data, out_fmt, compact=compact, table_fn=summarize_top)

        if action == "stats":
            data = client.get_json(
                f"/containers/{urllib.parse.quote(target, safe='')}/stats",
                query={"stream": "false"},
            )
            return format_output(data, out_fmt, compact=compact)

        if action in ("changes", "diff"):
            data = client.get_json(f"/containers/{urllib.parse.quote(target, safe='')}/changes")
            return format_output(data or [], out_fmt, compact=compact, table_fn=summarize_changes)

        raise DocatError(
            f"Unknown container action '{action}'. Supported: list, inspect, logs, top, stats, changes."
        )

    if resource == "images":
        if action in LIST_ACTIONS:
            query = {}
            if args.all:
                query["all"] = "1"
            if filters_json:
                query["filters"] = filters_json
            data = client.get_json("/images/json", query=query)
            return format_output(data, out_fmt, compact=compact, table_fn=summarize_images)

        if not target:
            raise DocatError(f"Image ID or name required for '{action}'.")

        if action in INSPECT_ACTIONS:
            data = client.get_json(f"/images/{urllib.parse.quote(target, safe='')}/json")
            return format_output(data, out_fmt, compact=compact)

        if action == "history":
            data = client.get_json(f"/images/{urllib.parse.quote(target, safe='')}/history")
            return format_output(data, out_fmt, compact=compact, table_fn=summarize_history)

        raise DocatError(f"Unknown image action '{action}'. Supported: list, inspect, history.")

    if resource == "networks":
        if action in LIST_ACTIONS:
            query = {"filters": filters_json} if filters_json else None
            data = client.get_json("/networks", query=query)
            return format_output(data, out_fmt, compact=compact, table_fn=summarize_networks)
        if action in INSPECT_ACTIONS:
            if not target:
                raise DocatError("Network ID or name required for inspect.")
            data = client.get_json(f"/networks/{urllib.parse.quote(target, safe='')}")
            return format_output(data, out_fmt, compact=compact)
        raise DocatError(f"Unknown network action '{action}'. Supported: list, inspect.")

    if resource == "volumes":
        if action in LIST_ACTIONS:
            query = {"filters": filters_json} if filters_json else None
            data = client.get_json("/volumes", query=query)
            return format_output(data, out_fmt, compact=compact, table_fn=summarize_volumes)
        if action in INSPECT_ACTIONS:
            if not target:
                raise DocatError("Volume name required for inspect.")
            data = client.get_json(f"/volumes/{urllib.parse.quote(target, safe='')}")
            return format_output(data, out_fmt, compact=compact)
        raise DocatError(f"Unknown volume action '{action}'. Supported: list, inspect.")

    if resource == "nodes":
        if action in LIST_ACTIONS:
            query = {"filters": filters_json} if filters_json else None
            data = client.get_json("/nodes", query=query)
            return format_output(data, out_fmt, compact=compact, table_fn=summarize_nodes)
        if action in INSPECT_ACTIONS:
            if not target:
                raise DocatError("Node ID or hostname required for inspect.")
            data = client.get_json(f"/nodes/{urllib.parse.quote(target, safe='')}")
            return format_output(data, out_fmt, compact=compact)
        raise DocatError(f"Unknown node action '{action}'. Supported: list, inspect.")

    if resource == "services":
        if action in LIST_ACTIONS:
            query = {"filters": filters_json} if filters_json else None
            data = client.get_json("/services", query=query)
            return format_output(data, out_fmt, compact=compact, table_fn=summarize_services)
        if not target:
            raise DocatError(f"Service ID or name required for '{action}'.")
        if action in INSPECT_ACTIONS:
            data = client.get_json(f"/services/{urllib.parse.quote(target, safe='')}")
            return format_output(data, out_fmt, compact=compact)
        if action == "logs":
            return _service_logs(client, target, args)
        raise DocatError(f"Unknown service action '{action}'. Supported: list, inspect, logs.")

    if resource == "tasks":
        if action in LIST_ACTIONS:
            query = {"filters": filters_json} if filters_json else None
            data = client.get_json("/tasks", query=query)
            return format_output(data, out_fmt, compact=compact, table_fn=summarize_tasks)
        if action in INSPECT_ACTIONS:
            if not target:
                raise DocatError("Task ID required for inspect.")
            data = client.get_json(f"/tasks/{urllib.parse.quote(target, safe='')}")
            return format_output(data, out_fmt, compact=compact)
        raise DocatError(f"Unknown task action '{action}'. Supported: list, inspect.")

    if resource == "configs":
        if action in LIST_ACTIONS:
            query = {"filters": filters_json} if filters_json else None
            data = client.get_json("/configs", query=query)
            return format_output(data, out_fmt, compact=compact, table_fn=summarize_configs_or_secrets)
        if action in INSPECT_ACTIONS:
            if not target:
                raise DocatError("Config ID or name required for inspect.")
            data = client.get_json(f"/configs/{urllib.parse.quote(target, safe='')}")
            return format_output(data, out_fmt, compact=compact)
        raise DocatError(f"Unknown config action '{action}'. Supported: list, inspect.")

    if resource == "secrets":
        if action in LIST_ACTIONS:
            query = {"filters": filters_json} if filters_json else None
            data = client.get_json("/secrets", query=query)
            return format_output(data, out_fmt, compact=compact, table_fn=summarize_configs_or_secrets)
        if action in INSPECT_ACTIONS:
            if not target:
                raise DocatError("Secret ID or name required for inspect.")
            data = client.get_json(f"/secrets/{urllib.parse.quote(target, safe='')}")
            return format_output(data, out_fmt, compact=compact)
        raise DocatError(f"Unknown secret action '{action}'. Supported: list, inspect.")

    if resource == "plugins":
        if action in LIST_ACTIONS:
            query = {"filters": filters_json} if filters_json else None
            data = client.get_json("/plugins", query=query)
            return format_output(data, out_fmt, compact=compact, table_fn=summarize_plugins)
        if action in INSPECT_ACTIONS:
            if not target:
                raise DocatError("Plugin name required for inspect.")
            data = client.get_json(f"/plugins/{urllib.parse.quote(target, safe='')}/json")
            return format_output(data, out_fmt, compact=compact)
        raise DocatError(f"Unknown plugin action '{action}'. Supported: list, inspect.")

    raise DocatError(f"Unhandled resource '{resource}'.")


def _resolve_action_and_target(rest: list[str]) -> tuple[str, str | None]:
    """Resolve [action, target] from positional tokens.

    Allows shorthand like:
    - `docat containers` -> ("list", None)
    - `docat containers ls` -> ("ls", None)
    - `docat containers inspect my-app` -> ("inspect", "my-app")
    - `docat containers my-app` -> ("inspect", "my-app")
    """
    if not rest:
        return "list", None
    known_actions = LIST_ACTIONS | INSPECT_ACTIONS | {"logs", "top", "stats", "changes", "diff", "history"}
    first = rest[0].lower()
    if first in known_actions:
        target = rest[1] if len(rest) > 1 else None
        return first, target
    # Shorthand: `docat container <id>` -> inspect <id>
    return "inspect", rest[0]


def _inspect_any(client: DockerClient, target: str, out_fmt: str, compact: bool) -> str:
    quoted = urllib.parse.quote(target, safe="")
    endpoints = [
        f"/containers/{quoted}/json",
        f"/images/{quoted}/json",
        f"/networks/{quoted}",
        f"/volumes/{quoted}",
        f"/services/{quoted}",
        f"/nodes/{quoted}",
        f"/tasks/{quoted}",
    ]
    last_err: DocatError | None = None
    for ep in endpoints:
        try:
            data = client.get_json(ep)
            return format_output(data, out_fmt, compact=compact)
        except DocatError as exc:
            last_err = exc
            if exc.status in (404, 503):
                continue
            raise
    raise last_err or DocatError(f"Object '{target}' not found.")


def _container_logs(client: DockerClient, target: str, args: argparse.Namespace) -> str:
    quoted = urllib.parse.quote(target, safe="")
    query: dict[str, Any] = {
        "stdout": "1",
        "stderr": "1",
        "tail": str(args.tail),
    }
    if args.timestamps:
        query["timestamps"] = "1"
    if args.since is not None:
        query["since"] = str(args.since)
    if args.until is not None:
        query["until"] = str(args.until)

    _, _, body = client.request_raw(f"/containers/{quoted}/logs", query=query)
    text = demux_docker_stream(body).rstrip("\n")
    if args.output == "json":
        return format_output({"container": target, "logs": text.splitlines() if text else []}, "json", compact=args.compact)
    if args.output in ("yaml", "yml"):
        return format_output({"container": target, "logs": text.splitlines() if text else []}, "yaml")
    return text


def _service_logs(client: DockerClient, target: str, args: argparse.Namespace) -> str:
    quoted = urllib.parse.quote(target, safe="")
    query: dict[str, Any] = {
        "stdout": "1",
        "stderr": "1",
        "tail": str(args.tail),
    }
    if args.timestamps:
        query["timestamps"] = "1"
    if args.since is not None:
        query["since"] = str(args.since)

    _, _, body = client.request_raw(f"/services/{quoted}/logs", query=query)
    text = demux_docker_stream(body).rstrip("\n")
    if args.output == "json":
        return format_output({"service": target, "logs": text.splitlines() if text else []}, "json", compact=args.compact)
    if args.output in ("yaml", "yml"):
        return format_output({"service": target, "logs": text.splitlines() if text else []}, "yaml")
    return text


# --- CLI Argument Parser ---


def env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def build_parser() -> argparse.ArgumentParser:
    default_host = os.environ.get("DOCAT_HOST") or os.environ.get("DOCKER_HOST") or DEFAULT_HOST
    default_output = (os.environ.get("DOCAT_OUTPUT") or os.environ.get("DOCAT_FORMAT") or "auto").lower()
    default_timeout = float(os.environ.get("DOCAT_TIMEOUT") or DEFAULT_TIMEOUT)
    default_api_version = os.environ.get("DOCAT_API_VERSION") or os.environ.get("DOCKER_API_VERSION")
    default_cert_path = os.environ.get("DOCAT_CERT_PATH") or os.environ.get("DOCKER_CERT_PATH")
    default_tls_verify = env_bool("DOCAT_TLS_VERIFY", env_bool("DOCKER_TLS_VERIFY", True))

    epilog = """examples:
  # List running or all containers
  docat containers
  docat ps -a
  docat containers -a -o json
  docat containers -o yaml

  # Inspect a container, image, network, volume, service, or task
  docat container inspect my-container
  docat image inspect nginx:latest -o yaml
  docat inspect my-container

  # Connect via Unix or TCP socket
  docat -H unix:///var/run/docker.sock containers
  docat -H tcp://127.0.0.1:2375 images
  DOCAT_HOST=tcp://10.0.0.5:2375 DOCAT_OUTPUT=yaml docat nodes

  # Swarm resources (swarm, nodes, services, tasks, configs, secrets)
  docat swarm
  docat nodes
  docat services
  docat tasks -f desired-state=running
  docat task inspect <task-id> -o yaml

  # Container logs, top, stats, diff & Engine info
  docat container logs my-container --tail 50
  docat container top my-container
  docat container stats my-container -o yaml
  docat info -o yaml
  docat df
  docat raw /version
"""

    parser = argparse.ArgumentParser(
        prog="docat",
        description="Read-only CLI wrapper for the Docker Engine socket (Unix & TCP).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="Resource and action (e.g. 'containers [list|inspect|logs|top|stats]', 'images', 'networks', 'volumes', 'swarm', 'nodes', 'services', 'tasks', 'info', 'version', 'df', 'raw <path>')",
    )
    parser.add_argument(
        "-H",
        "--host",
        default=default_host,
        help=f"Docker socket to connect to, e.g. unix:///var/run/docker.sock or tcp://host:2375 (env: DOCAT_HOST, DOCKER_HOST; default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "-o",
        "--output",
        "--format",
        dest="output",
        choices=["auto", "table", "json", "yaml", "yml"],
        default=default_output if default_output in ("auto", "table", "json", "yaml", "yml") else "auto",
        help="Output format: auto (table for lists, json for inspect), table, json, or yaml (env: DOCAT_OUTPUT)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        default=env_bool("DOCAT_COMPACT", False),
        help="Emit single-line compact JSON when using -o json (env: DOCAT_COMPACT)",
    )
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        default=env_bool("DOCAT_ALL", False),
        help="Show all items (e.g. include stopped containers or intermediate images; env: DOCAT_ALL)",
    )
    parser.add_argument(
        "-f",
        "--filter",
        action="append",
        metavar="KEY=VAL",
        help="Filter output (can be specified multiple times, e.g. -f status=running)",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=None,
        help="Return this number of most recently created containers",
    )
    parser.add_argument(
        "-s",
        "--size",
        action="store_true",
        help="Include container file sizes",
    )
    parser.add_argument(
        "--tail",
        default=os.environ.get("DOCAT_TAIL", "100"),
        help="Number of lines to show from the end of logs (default: 100, or 'all'; env: DOCAT_TAIL)",
    )
    parser.add_argument(
        "-t",
        "--timestamps",
        action="store_true",
        help="Show timestamps in logs",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Show logs or events since timestamp (Unix epoch seconds)",
    )
    parser.add_argument(
        "--until",
        default=None,
        help="Show logs or events until timestamp (Unix epoch seconds)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=default_timeout,
        help=f"Socket timeout in seconds (env: DOCAT_TIMEOUT; default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--api-version",
        default=default_api_version,
        help="Optional Docker API version prefix, e.g. 1.45 (env: DOCAT_API_VERSION, DOCKER_API_VERSION)",
    )
    parser.add_argument(
        "--cert-path",
        default=default_cert_path,
        help="Path to TLS certificates directory (ca.pem, cert.pem, key.pem) (env: DOCAT_CERT_PATH, DOCKER_CERT_PATH)",
    )
    parser.add_argument(
        "--no-tls-verify",
        dest="tls_verify",
        action="store_false",
        default=default_tls_verify,
        help="Skip TLS certificate verification for HTTPS connections",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.command:
        parser.print_help()
        return 0

    client = DockerClient(
        host=args.host,
        timeout=args.timeout,
        api_version=args.api_version,
        cert_path=args.cert_path,
        tls_verify=args.tls_verify,
    )

    try:
        output = execute_command(client, args)
        if output:
            print(output)
        return 0
    except DocatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
