"""Proveedores SOLO sin censura con failover automático. Cero OpenRouter.

Orden por defecto:
  1) AI Horde  -> modelos uncensored/heretic/abliterated de la comunidad (GRATIS, sin cuenta)
  2) Venice    -> venice-uncensored y variantes uncensored (de pago, solo si pones VENICE_API_KEY)
  3) Unlid     -> uncensored/abliterated (de pago, solo si pones UNLID_API_KEY)
  4) Heabsy    -> uncensored (de pago, solo si pones HEABSY_API_KEY)
  5) Refuseless-> abliterated (de pago, solo si pones REFUSELESS_API_KEY)
  6) extra     -> cualquier API compatible con OpenAI (EXTRA_BASE_URL + EXTRA_API_KEY + EXTRA_MODELS)
Todo ID que no sea uncensored/abliterated/heretic se descarta, aunque venga de una variable.
"""
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

# Proveedor extra opcional (cualquier API compatible con OpenAI y tier gratis). Ej: EXTRA_BASE_URL=https://.../v1
EXTRA_BASE_URL = os.getenv("EXTRA_BASE_URL", "").rstrip("/")
EXTRA_API_KEY = os.getenv("EXTRA_API_KEY", "")
EXTRA_MODELS = [m.strip() for m in os.getenv("EXTRA_MODELS", "").split(",") if m.strip()]
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
    "koboldcpp/L3-8B-Stheno-v3.2",
    "koboldcpp/gemma-3-4b-it-heretic",
]
if os.getenv("HORDE_MODELS"):
    HORDE_MODELS = [m.strip() for m in os.getenv("HORDE_MODELS").split(",") if m.strip()]

PROVIDER_ORDER = [p.strip().lower() for p in os.getenv("PROVIDER_ORDER", "horde,venice,unlid,heabsy,refuseless,extra").split(",") if p.strip()]
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
                        r"rocinante|wicked|dark|magnum|unleashed|impish|orthia|nocturne|drummer|lumimaid|"
                        r"midnight|mythomax|lewd|nsfw|erebus|pygmalion|euryale|celeste|-abl\b", re.I)
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


HORDE_MODELS = only_uncensored(HORDE_MODELS)
EXTRA_MODELS = only_uncensored(EXTRA_MODELS)


def _env_models(name, default):
    return only_uncensored([m.strip() for m in os.getenv(name, default).split(",") if m.strip()])


# Proveedores de pago SIN censura. Solo se usan si pones su API key. IDs de Venice verificados en su catálogo
# público (16-11-2026); los de Unlid/Heabsy/Refuseless salen de tu catálogo: confírmalos con GET /v1/models.
PAID = {
    "venice": {"url": "https://api.venice.ai/api/v1", "key": os.getenv("VENICE_API_KEY", ""),
               "models": _env_models("VENICE_MODELS",
                   "venice-uncensored-1-2,deepseek-v4-1-flash-1017-uncensored,glm-5-3-flash-uncensored,"
                   "gemma-4-uncensored,olafangensan-glm-4.7-flash-heretic,venice-uncensored-role-play,"
                   "abliteration-abliterated-model-large-flash")},
    "unlid": {"url": "https://api.unlid.ai/v1", "key": os.getenv("UNLID_API_KEY", ""),
              "models": _env_models("UNLID_MODELS",
                  "deepseek-v4.1-flash-uncensored,glm-5.3-uncensored,qwen3.8-27b-uncensored,"
                  "glm-5.3-flash-abliterated,gemma-4-31b-uncensored,dolphin-mistral-24b-uncensored,"
                  "deepseek-v4.1-flash-heretic,supergemma4-26b-uncensored")},
    "heabsy": {"url": "https://api.heabsy.com/v1", "key": os.getenv("HEABSY_API_KEY", ""),
               "models": _env_models("HEABSY_MODELS",
                   "deepseek-v4.1-flash-unc,glm53-flash-unc,qwen38-unc,mimo-v26-flash-unc,mimo-v26-flash-abl")},
    "refuseless": {"url": "https://api.refuseless.com/v1", "key": os.getenv("REFUSELESS_API_KEY", ""),
                   "models": _env_models("REFUSELESS_MODELS",
                       "glm-5.3-abliterated,deepseek-v4.1-flash-abliterated,glm-5.3-flash-abliterated")},
}

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


async def _try_paid(session, prov, model, messages, max_tokens, temperature):
    cfg = PAID[prov]
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if prov == "venice":  # quita el prompt de sistema propio de Venice para que mande el nuestro
        payload["venice_parameters"] = {"include_venice_system_prompt": False}
    try:
        status, body, content = await _post(session, f"{cfg['url']}/chat/completions", cfg["key"], payload, 120)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise LLMError(str(e) or type(e).__name__)
    if content:
        return content
    raise LLMError(f"HTTP {status}: {body[:150]}")


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
        if provider in PAID:
            if not (PAID[provider]["key"] and PAID[provider]["models"]):
                continue
            candidates = [(provider, m) for m in PAID[provider]["models"]]
        elif provider == "horde":
            candidates = [("horde", m) for m in await horde_candidates(session, mode)]
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
                if prov in PAID:
                    text = await _try_paid(session, prov, model, messages, max_tokens, temperature)
                else:
                    fn = _try_extra if prov == "extra" else _try_horde
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
    raise LLMError("Ningún modelo sin censura respondió. Últimos errores: " + " | ".join(errors[-3:]))
