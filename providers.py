"""Solo usa modelos gratuitos: AI Horde y modelos OpenRouter marcados :free."""
import asyncio
import json
import logging
import os
import re
import time

import aiohttp

log = logging.getLogger("dolphin.providers")

HORDE_BASE = "https://oai.aihorde.net/v1"
HORDE_STATUS_URL = "https://aihorde.net/api/v2/status/models?type=text"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODELS = [
    "cognitivecomputations/dolphin-mistral-24b-venice-edition:free"
]
if os.getenv("OPENROUTER_MODELS"):
    OPENROUTER_MODELS = [m.strip() for m in os.getenv("OPENROUTER_MODELS").split(",") if m.strip()]
HORDE_API_KEY = os.getenv("HORDE_API_KEY", "0000000000")  # anónima; con cuenta gratis tienes más prioridad


# Catálogo AI Horde (orden = preferencia). Se filtra con los que estén online en ese momento.
HORDE_MODELS = [
    "aphrodite/TheDrummer/Behemoth-128B-v3",
    "aphrodite/TheDrummer/Skyfall-31B-v4.2",
    "Nimbz/Gemma-4-Dark-Orthia-31B",
    "ReadyArt/Wicked-Tutu-31B-Final",
    "koboldcpp/G4-MeroMero-26B-A4B-it-uncensored-heretic",
    "koboldcpp/Rocinante-X-12B-v1",
    "koboldcpp/Cydonia-24B-v4zg-Q4_K_M",
    "koboldcpp/NemoMix-Unleashed-12B-Q6_K",
    "sonar/TheDrummer/Behemoth-128B-v3-4.25bpw-h6-exl3",
    "koboldcpp/Skyfall-31B-v4.2",
    "koboldcpp/Sao10K/L3-8B-Stheno-v3.2",
    "koboldcpp/mini-magnum-12b-v1.1",
    "koboldcpp/LFM3-1.7B-Uncensored-AD-Q6_K",
    "koboldcpp/Mistral-Nemo-2407-Thinking-Claude-Gemini-GPT5.2-Uncensored-HERETIC",
    "koboldcpp/gemma-4-31B-it-heretic",
    "koboldcpp/Gemma-4-E4B-it-Ultra-Uncensored-Heretic",
    "koboldcpp/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q4_K_M",
    "koboldcpp/Cydonia-24B-v4.3-heretic-Q4_K_M",
    "Qwen3.8-Flash-Next-Uncensored-NVFP4",
    "koboldcpp/G4-MeroMero-26B-A4B-it-uncensored-heretic",
    "koboldcpp/L3-8B-Stheno-v3.2",
    "koboldcpp/gemma-3-4b-it-heretic",
]
if os.getenv("HORDE_MODELS"):
    HORDE_MODELS = [m.strip() for m in os.getenv("HORDE_MODELS").split(",") if m.strip()]

PROVIDER_ORDER = ["horde", "openrouter"]
HORDE_MAX_TOKENS = int(os.getenv("HORDE_MAX_TOKENS", "1024"))            # el tier anónimo limita la longitud
HORDE_MAX_CONTEXT_CHARS = int(os.getenv("HORDE_MAX_CONTEXT_CHARS", "10000"))
HORDE_TIMEOUT = int(os.getenv("HORDE_TIMEOUT", "180"))
TOTAL_TIMEOUT = int(os.getenv("LLM_TOTAL_TIMEOUT", "600"))

# Orden por modo. chat = rápido y coloquial; code = el más grande/inteligente primero.
HORDE_BIG = "koboldcpp/gemma-4-31B-it-heretic"
HORDE_CHAT_FIRST = ["koboldcpp/Mistral-Nemo-2407-Thinking-Claude-Gemini-GPT5.2-Uncensored-HERETIC",
                    "koboldcpp/L3-8B-Stheno-v3.2"]

# Solo nombres identificados como uncensored, heretic, abliteration o variantes afines.
ALLOWED_RE = re.compile(r"uncensored|heretic|abliterat|dolphin|stheno|[-_]unc\b|hauhaucs|behemoth|skyfall|cydonia|"
                        r"rocinante|wicked|dark|magnum|unleashed|impish|orthia|nocturne|drummer|lumimaid|"
                        r"midnight|mythomax|lewd|nsfw|erebus|pygmalion|euryale|celeste|-abl\b", re.I)


def only_uncensored(models):
    ok = [m for m in models if ALLOWED_RE.search(m)]
    for m in models:
        if m not in ok:
            log.warning("Descartado (no es uncensored): %s", m)
    return ok


HORDE_MODELS = only_uncensored(HORDE_MODELS)
OPENROUTER_MODELS = [
    model for model in OPENROUTER_MODELS
    if model.lower().endswith(":free") and ALLOWED_RE.search(model)
]
_openrouter_cache = {"t": 0.0, "models": None}

_REPEAT_RE = re.compile(r"(.{2,40}?)\1{6,}", re.S)
_LATIN_RE = re.compile(r"[A-Za-z0-9\u00C0-\u024F\s\.,;:!?¡¿'\"()\[\]{}<>/\\\-_=+*#@$%&|`~^]")


def is_garbage(text):
    """Detecta salidas rotas: bucles tipo 'AppAppApp', otro alfabeto, casi sin palabras distintas."""
    t = text.strip()
    if len(t) < 2:
        return True
    if _REPEAT_RE.search(t):
        return True
    latin = sum(1 for c in t if _LATIN_RE.match(c))
    if latin / len(t) < 0.75:
        return True
    words = t.lower().split()
    if len(words) >= 12 and len(set(words)) / len(words) < 0.25:
        return True
    return False


def _ordered(models, mode):
    first = [m for m in (HORDE_CHAT_FIRST if mode == "chat" else [HORDE_BIG]) if m in models]
    return first + [m for m in models if m not in first]


_horde_cache = {"t": 0.0, "online": None}


class LLMError(Exception):
    pass


async def horde_online_models(session):
    """Devuelve el set de modelos con workers activos (cache 5 min). None si no se pudo consultar."""
    if time.time() - _horde_cache["t"] < 300 and _horde_cache["online"] is not None:
        return _horde_cache["online"]
    try:
        async with session.get(HORDE_STATUS_URL, headers={"Client-Agent": "dolphin-discord-bot:1.0:unknown"},
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            data = await r.json()
        online = {m.get("name") for m in data if m.get("count", 0) > 0}
        _horde_cache.update(t=time.time(), online=online)
        return online
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
        return None


async def openrouter_free_models(session):
    """Lee el catálogo público y acepta solo IDs :free con nombre uncensored verificado."""
    if not OPENROUTER_API_KEY:
        return []
    if time.time() - _openrouter_cache["t"] < 300 and _openrouter_cache["models"] is not None:
        return _openrouter_cache["models"]
    try:
        async with session.get(
            f"{OPENROUTER_BASE}/models",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            if response.status != 200:
                return list(OPENROUTER_MODELS)
            data = await response.json()
        found = [
            item.get("id", "")
            for item in data.get("data", [])
            if item.get("id", "").lower().endswith(":free")
            and ALLOWED_RE.search(item.get("id", ""))
        ]
        models = found or list(OPENROUTER_MODELS)
        _openrouter_cache.update(t=time.time(), models=models)
        return models
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
        return list(OPENROUTER_MODELS)


def _trim_for_horde(messages):
    """Los workers de Horde tienen contexto corto: quita historial viejo y recorta el último mensaje."""
    system, rest = messages[0], messages[1:]
    last = dict(rest[-1])
    hist = list(rest[:-1])
    budget = HORDE_MAX_CONTEXT_CHARS
    sys_c = system["content"][: budget // 2]
    budget -= len(sys_c)
    if len(last["content"]) > budget:
        last["content"] = last["content"][: budget - 200] + "\n[...recortado por límite de contexto...]"
    budget -= len(last["content"])
    keep = []
    for m in reversed(hist):
        if len(m["content"]) > budget:
            break
        budget -= len(m["content"])
        keep.append(m)
    keep.reverse()
    while keep and keep[0]["role"] != "user":
        keep.pop(0)
    return [{"role": "system", "content": sys_c}, *keep, last]


async def _post(session, url, key, payload, timeout, extra_headers=None):
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        body = await r.text()
        if r.status != 200:
            return r.status, body, None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return 200, body, None
        choices = data.get("choices")
        if choices:
            content = (choices[0].get("message") or {}).get("content")
            if content and content.strip():
                return 200, body, content
        return 200, body, None


async def _try_openrouter(session, model, messages, max_tokens, temperature):
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    try:
        status, body, content = await _post(
            session, f"{OPENROUTER_BASE}/chat/completions", OPENROUTER_API_KEY, payload, 120
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise LLMError(str(e) or type(e).__name__)
    if content:
        return content
    raise LLMError(f"HTTP {status}: {body[:150]}")


def _size(name):
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b", name)
    return float(m.group(1)) if m else 0.0


async def horde_candidates(session, mode):
    """Modelos de Horde online: los de mi lista + cualquier uncensored/heretic/abliterated nuevo que aparezca."""
    online = await horde_online_models(session)
    if online is None:
        return list(HORDE_MODELS)
    found = [m for m in online if m and ALLOWED_RE.search(m)]
    known = [m for m in HORDE_MODELS if m in online]
    new = sorted((m for m in found if m not in known), key=_size, reverse=True)
    allm = known + new
    if mode == "work":
        allm.sort(key=_size, reverse=True)       # código: el más grande primero
    else:
        allm = _ordered(allm, mode)              # charla: los coherentes y rápidos primero
    return allm or list(HORDE_MODELS)


async def _try_horde(session, model, messages, max_tokens, temperature):
    payload = {"model": model, "messages": _trim_for_horde(messages),
               "temperature": temperature, "max_tokens": min(max_tokens, HORDE_MAX_TOKENS)}
    try:
        status, body, content = await _post(session, f"{HORDE_BASE}/chat/completions", HORDE_API_KEY,
                                            payload, HORDE_TIMEOUT)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise LLMError(str(e) or type(e).__name__)
    if content:
        return content
    raise LLMError(f"HTTP {status}: {body[:150]}")


async def chat(session, messages, max_tokens=4096, temperature=0.7, mode="work"):
    """Prueba proveedores gratuitos; una negativa es respuesta, no motivo para eludirla."""
    deadline = time.time() + TOTAL_TIMEOUT
    errors = []
    candidates = [("horde", model) for model in await horde_candidates(session, mode)]
    candidates += [("openrouter", model) for model in await openrouter_free_models(session)]
    for prov, model in candidates:
        if time.time() > deadline:
            raise LLMError("Se acabó el tiempo probando modelos gratis: " + " | ".join(errors[-3:]))
        try:
            fn = _try_openrouter if prov == "openrouter" else _try_horde
            text = await fn(session, model, messages, max_tokens, temperature)
            if is_garbage(text):
                raise LLMError("salida basura (bucle o alfabeto raro)")
            log.info("Respondió %s:%s", prov, model)
            return text, f"{prov}:{model}"
        except LLMError as e:
            log.warning("Falló %s:%s -> %s", prov, model, e)
            errors.append(f"{model.split('/')[-1]}: {e}")
    raise LLMError("Ningún modelo gratuito del catálogo respondió. Últimos errores: " + " | ".join(errors[-3:]))
