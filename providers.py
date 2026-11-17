"""Proveedores 100% gratuitos con failover automático.

Orden por defecto:
  1) OpenRouter  -> cognitivecomputations/dolphin-mistral-24b-venice-edition:free  (solo si hay OPENROUTER_API_KEY, que es gratis)
  2) AI Horde    -> todos los modelos uncensored/heretic del catálogo (sin cuenta, key anónima 0000000000)

Los modelos de pago (Unlid, Heabsy, Refuseless) y Venice (necesita créditos) NO se usan a propósito.
"""
import asyncio
import json
import logging
import os
import re
import time

import aiohttp

log = logging.getLogger("dolphin.providers")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HORDE_BASE = "https://oai.aihorde.net/v1"
HORDE_STATUS_URL = "https://aihorde.net/api/v2/status/models?type=text"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
# Proveedor extra opcional (cualquier API compatible con OpenAI y tier gratis). Ej: EXTRA_BASE_URL=https://.../v1
EXTRA_BASE_URL = os.getenv("EXTRA_BASE_URL", "").rstrip("/")
EXTRA_API_KEY = os.getenv("EXTRA_API_KEY", "")
EXTRA_MODELS = [m.strip() for m in os.getenv("EXTRA_MODELS", "").split(",") if m.strip()]
HORDE_API_KEY = os.getenv("HORDE_API_KEY", "0000000000")  # anónima; con cuenta gratis tienes más prioridad

OPENROUTER_MODELS = [
    m.strip() for m in os.getenv(
        "OPENROUTER_MODEL", ""
    ).split(",") if m.strip()
]

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
    "koboldcpp/Mistral-Nemo-2407-Thinking-Claude-Gemini-GPT5.2-Uncensored-HERETIC",
    "koboldcpp/gemma-4-31B-it-heretic",
    "koboldcpp/Gemma-4-E4B-it-Ultra-Uncensored-Heretic",
    "koboldcpp/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q4_K_M",
    "koboldcpp/L3-8B-Stheno-v3.2",
    "koboldcpp/gemma-3-4b-it-heretic",
]
if os.getenv("HORDE_MODELS"):
    HORDE_MODELS = [m.strip() for m in os.getenv("HORDE_MODELS").split(",") if m.strip()]

PROVIDER_ORDER = [p.strip().lower() for p in os.getenv("PROVIDER_ORDER", "openrouter,horde,fallback").split(",") if p.strip()]
HORDE_MAX_TOKENS = int(os.getenv("HORDE_MAX_TOKENS", "1024"))            # el tier anónimo limita la longitud
HORDE_MAX_CONTEXT_CHARS = int(os.getenv("HORDE_MAX_CONTEXT_CHARS", "10000"))
HORDE_TIMEOUT = int(os.getenv("HORDE_TIMEOUT", "180"))
TOTAL_TIMEOUT = int(os.getenv("LLM_TOTAL_TIMEOUT", "600"))

# Orden por modo. chat = rápido y coloquial; code = el más grande/inteligente primero.
HORDE_BIG = "koboldcpp/gemma-4-31B-it-heretic"
HORDE_CHAT_FIRST = ["koboldcpp/Mistral-Nemo-2407-Thinking-Claude-Gemini-GPT5.2-Uncensored-HERETIC",
                    "koboldcpp/L3-8B-Stheno-v3.2"]

# Solo modelos sin censura: cualquier ID que no cuadre se descarta, aunque venga de una variable de entorno.
ALLOWED_RE = re.compile(r"uncensored|heretic|abliterat|dolphin|stheno|[-_]unc\b|hauhaucs|behemoth|skyfall|cydonia|"
                        r"rocinante|wicked|dark|magnum|unleashed|impish|orthia|nocturne|drummer", re.I)
REFUSAL_RE = re.compile(
    r"^\W*(lo siento|perd[oó]n|disculpa|no puedo|no me es posible|lamentablemente|i'?m sorry|i am sorry|sorry|"
    r"i can(?:'|no)t|i cannot|i won'?t|as an ai|como (?:una )?(?:ia|inteligencia artificial|modelo de lenguaje))", re.I)


def is_refusal(text):
    return bool(REFUSAL_RE.match(text.strip()[:160]))


def only_uncensored(models):
    ok = [m for m in models if ALLOWED_RE.search(m)]
    for m in models:
        if m not in ok:
            log.warning("Descartado (no es uncensored): %s", m)
    return ok


OPENROUTER_MODELS = only_uncensored(OPENROUTER_MODELS)
# Último recurso si todos los sin censura fallan. Cámbialos con OPENROUTER_FALLBACK (separados por coma).
OPENROUTER_FALLBACK = [m.strip() for m in os.getenv(
    "OPENROUTER_FALLBACK",
    "nvidia/nemotron-3-ultra-550b-a55b:free,google/gemma-4-31b-it:free,nvidia/nemotron-3.5-super-vl:free,"
    "cohere/north-mini-code:free,google/gemma-4-26b-a4b-it:free").split(",") if m.strip()]
HORDE_MODELS = only_uncensored(HORDE_MODELS)
EXTRA_MODELS = only_uncensored(EXTRA_MODELS)

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
    extra = {"HTTP-Referer": "https://replit.com", "X-Title": "Dolphin Discord Bot"}
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    last = "desconocido"
    for attempt in range(2):
        try:
            status, body, content = await _post(session, OPENROUTER_URL, OPENROUTER_API_KEY, payload, 120, extra)
            if content:
                return content
            last = f"HTTP {status}: {body[:150]}"
            if status not in (200, 429, 500, 502, 503, 504):
                break  # 401/402/404... no tiene sentido reintentar
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = str(e) or type(e).__name__
        await asyncio.sleep(3 * (2 ** attempt))
    raise LLMError(last)


async def _try_extra(session, model, messages, max_tokens, temperature):
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    try:
        status, body, content = await _post(session, f"{EXTRA_BASE_URL}/chat/completions", EXTRA_API_KEY, payload, 120)
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
    """Prueba cada modelo gratis en orden hasta que uno responda. Devuelve (texto, 'proveedor:modelo')."""
    deadline = time.time() + TOTAL_TIMEOUT
    errors = []
    refused = None
    order = list(PROVIDER_ORDER)
    if "extra" not in order and EXTRA_BASE_URL and EXTRA_MODELS:
        order.append("extra")
    for provider in order:
        if provider == "openrouter":
            if not OPENROUTER_API_KEY:
                continue
            candidates = [("openrouter", m) for m in OPENROUTER_MODELS]
        elif provider == "horde":
            candidates = [("horde", m) for m in await horde_candidates(session, mode)]
        elif provider == "fallback":
            if not OPENROUTER_API_KEY:
                continue
            candidates = [("openrouter", m) for m in OPENROUTER_FALLBACK]
        elif provider == "extra":
            if not (EXTRA_BASE_URL and EXTRA_MODELS):
                continue
            candidates = [("extra", m) for m in EXTRA_MODELS]
        else:
            continue
        for prov, model in candidates:
            if time.time() > deadline:
                raise LLMError("Se acabó el tiempo probando modelos gratis: " + " | ".join(errors[-3:]))
            try:
                fn = {"openrouter": _try_openrouter, "extra": _try_extra}.get(prov, _try_horde)
                text = await fn(session, model, messages, max_tokens, temperature)
                if is_garbage(text):
                    raise LLMError("salida basura (bucle o alfabeto raro)")
                if is_refusal(text):  # se negó: prueba el siguiente modelo sin censura
                    log.warning("Rechazó %s:%s", prov, model)
                    refused = refused or (text, f"{prov}:{model}")
                    errors.append(f"{model.split('/')[-1]}: se negó")
                    continue
                log.info("Respondió %s:%s", prov, model)
                return text, f"{prov}:{model}"
            except LLMError as e:
                log.warning("Falló %s:%s -> %s", prov, model, e)
                errors.append(f"{model.split('/')[-1]}: {e}")
    if refused:
        return refused
    raise LLMError("Ningún modelo gratis respondió. Últimos errores: " + " | ".join(errors[-3:]))
