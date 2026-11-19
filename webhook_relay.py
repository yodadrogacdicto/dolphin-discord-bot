"""A rate-limited, origin-allowlisted relay from a static site to Discord."""
import asyncio
import os
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import aiohttp
from aiohttp import web

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
WEBHOOK_ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.getenv("WEBHOOK_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}
MAX_MESSAGE_LENGTH = 1900
MAX_REQUEST_BYTES = 8192
RATE_LIMIT_PER_MINUTE = 10
_requests_by_ip = defaultdict(deque)
_requests_global = deque()


def _valid_webhook_url(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        parts = [part for part in parsed.path.split("/") if part]
        return (
            parsed.scheme == "https"
            and host in {"discord.com", "discordapp.com"}
            and len(parts) == 4
            and parts[:2] == ["api", "webhooks"]
            and bool(parts[2])
            and bool(parts[3])
            and not parsed.username
            and not parsed.password
        )
    except (TypeError, ValueError):
        return False


def _message_from_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("Se esperaba un objeto JSON.")
    content = payload.get("content")
    if content is None:
        labels = (("name", "Nombre"), ("email", "Correo"), ("subject", "Asunto"), ("message", "Mensaje"))
        parts = [
            f"{label}: {str(payload[key]).strip()[:500]}"
            for key, label in labels
            if payload.get(key) is not None and str(payload[key]).strip()
        ]
        content = "\n".join(parts)
    if not isinstance(content, str):
        raise ValueError("El campo content debe ser texto.")
    content = content.strip()
    if not content:
        raise ValueError("El mensaje está vacío.")
    if len(content) > MAX_MESSAGE_LENGTH:
        raise ValueError(f"El mensaje supera el límite de {MAX_MESSAGE_LENGTH} caracteres.")
    return content


def _is_allowed_origin(origin):
    return bool(origin) and origin.rstrip("/") in WEBHOOK_ALLOWED_ORIGINS


def _apply_cors(response, origin):
    if _is_allowed_origin(origin):
        response.headers["Access-Control-Allow-Origin"] = origin.rstrip("/")
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Max-Age"] = "600"
        response.headers["Vary"] = "Origin"
    return response


def _within_rate_limit(remote):
    now = time.monotonic()
    cutoff = now - 60
    while _requests_global and _requests_global[0] < cutoff:
        _requests_global.popleft()
    for key, events in list(_requests_by_ip.items()):
        while events and events[0] < cutoff:
            events.popleft()
        if not events:
            del _requests_by_ip[key]
    key = remote or "unknown"
    per_ip = _requests_by_ip.get(key, deque())
    while per_ip and per_ip[0] < cutoff:
        per_ip.popleft()
    if len(_requests_global) >= RATE_LIMIT_PER_MINUTE * 6 or len(per_ip) >= RATE_LIMIT_PER_MINUTE:
        return False
    _requests_global.append(now)
    _requests_by_ip[key] = per_ip
    per_ip.append(now)
    return True


async def _send_to_discord(session, content):
    parsed = urlsplit(DISCORD_WEBHOOK_URL)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["wait"] = "true"
    target = urlunsplit(parsed._replace(query=urlencode(query)))
    payload = {"content": content, "allowed_mentions": {"parse": []}}
    try:
        async with session.post(
            target,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=12),
            allow_redirects=False,
        ) as response:
            if response.status in (200, 201, 204):
                return True, response.status
            if response.status == 429:
                return False, 429
            return False, response.status
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False, 502


async def handle_options(request):
    origin = request.headers.get("Origin", "")
    if not _is_allowed_origin(origin):
        return web.json_response({"error": "Origen no permitido."}, status=403)
    return _apply_cors(web.Response(status=204), origin)


async def handle_post(request, session):
    origin = request.headers.get("Origin", "")
    if not _is_allowed_origin(origin):
        return web.json_response({"error": "Origen no permitido."}, status=403)
    if not DISCORD_WEBHOOK_URL or not _valid_webhook_url(DISCORD_WEBHOOK_URL):
        return _apply_cors(
            web.json_response({"error": "El relay de Discord no está configurado."}, status=503),
            origin,
        )
    if request.content_length and request.content_length > MAX_REQUEST_BYTES:
        return _apply_cors(web.json_response({"error": "Solicitud demasiado grande."}, status=413), origin)
    if request.content_type != "application/json":
        return _apply_cors(web.json_response({"error": "Envía JSON."}, status=415), origin)
    try:
        payload = await request.json()
        content = _message_from_payload(payload)
    except (ValueError, TypeError):
        return _apply_cors(web.json_response({"error": "JSON o mensaje no válido."}, status=400), origin)
    if not _within_rate_limit(request.remote):
        return _apply_cors(web.json_response({"error": "Límite temporal de envíos alcanzado."}, status=429), origin)

    sent, status = await _send_to_discord(session, content)
    if not sent:
        message = "Discord rechazó el envío." if status != 502 else "No se pudo conectar con Discord."
        return _apply_cors(web.json_response({"error": message}, status=status), origin)
    return _apply_cors(web.json_response({"ok": True, "status": status}), origin)
