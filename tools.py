"""Herramientas del bot: búsqueda web, GitHub y lectura de URLs."""
import asyncio
import ipaddress
import os
import re
import signal
import socket
import sys
import tempfile
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

import filereader

UA = "Mozilla/5.0 (compatible; DolphinDiscordBot/1.0)"
MAX_CHARS = 12000
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")


def _gh_headers(raw=False):
    h = {
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "User-Agent": UA,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


# ---------------------------------------------------------------- web search
def _ddg_sync(query, n=6):
    try:
        from ddgs import DDGS
    except ImportError:  # paquete antiguo
        from duckduckgo_search import DDGS
    with DDGS() as d:
        return list(d.text(query, max_results=n))


async def web_search(query):
    results = await asyncio.to_thread(_ddg_sync, query)
    if not results:
        return "Sin resultados."
    lines = []
    for r in results:
        lines.append(f"- {r.get('title','')}\n  {r.get('href') or r.get('url','')}\n  {r.get('body','')}")
    return "\n".join(lines)[:MAX_CHARS]


# -------------------------------------------------------------------- GitHub
async def github_repos(query):
    params = {"q": query, "sort": "stars", "order": "desc", "per_page": "7"}
    async with aiohttp.ClientSession(headers=_gh_headers()) as s:
        async with s.get("https://api.github.com/search/repositories", params=params,
                         timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status != 200:
                return f"GitHub respondió HTTP {r.status}: {(await r.text())[:200]}"
            data = await r.json()
    items = data.get("items", [])
    if not items:
        return "Sin repositorios."
    return "\n".join(
        f"- {i['full_name']} ★{i['stargazers_count']} [{i.get('language')}] {i['html_url']}\n"
        f"  {i.get('description') or ''} (actualizado {i['pushed_at'][:10]})"
        for i in items
    )


async def github_code(query):
    if not GITHUB_TOKEN:
        return "La búsqueda de código en GitHub requiere GITHUB_TOKEN (no configurado). Usa GITHUB o SEARCH."
    params = {"q": query, "per_page": "7"}
    async with aiohttp.ClientSession(headers=_gh_headers()) as s:
        async with s.get("https://api.github.com/search/code", params=params,
                         timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status != 200:
                return f"GitHub respondió HTTP {r.status}: {(await r.text())[:200]}"
            data = await r.json()
    items = data.get("items", [])
    if not items:
        return "Sin resultados de código."
    return "\n".join(f"- {i['repository']['full_name']} :: {i['path']}\n  {i['html_url']}" for i in items)


async def github_overview(owner, repo):
    out = []
    async with aiohttp.ClientSession(headers=_gh_headers(raw=True)) as s:
        async with s.get(f"https://api.github.com/repos/{owner}/{repo}/readme",
                         timeout=aiohttp.ClientTimeout(total=25)) as r:
            out.append("README:\n" + (await r.text())[:6000] if r.status == 200 else f"(sin README, HTTP {r.status})")
    async with aiohttp.ClientSession(headers=_gh_headers()) as s:
        async with s.get(f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1",
                         timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status == 200:
                tree = (await r.json()).get("tree", [])
                paths = [t["path"] for t in tree if t["type"] == "blob"][:250]
                out.append("ARCHIVOS:\n" + "\n".join(paths))
    return "\n\n".join(out)[:MAX_CHARS]


# --------------------------------------------------------------------- fetch
async def _is_safe(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, p.hostname, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


async def fetch_url(url):
    url = url.strip().strip("<>")
    m = re.match(r"https?://github\.com/([^/\s]+)/([^/\s#?]+)/?$", url)
    if m:
        repo = m.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return await github_overview(m.group(1), repo)
    url = re.sub(r"^https?://github\.com/([^/]+)/([^/]+)/blob/",
                 r"https://raw.githubusercontent.com/\1/\2/", url)
    current = url
    async with aiohttp.ClientSession(headers={"User-Agent": UA}) as s:
        for _ in range(4):
            if not await _is_safe(current):
                return "URL bloqueada (no es http/https público)."
            async with s.get(current, allow_redirects=False,
                             timeout=aiohttp.ClientTimeout(total=25)) as r:
                if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    current = urljoin(current, r.headers["Location"])
                    continue
                if r.status >= 400:
                    return f"HTTP {r.status}"
                ctype = r.headers.get("Content-Type", "")
                data = await r.content.read(2_000_000)
                text = data.decode(r.charset or "utf-8", errors="replace")
                if "html" in ctype.lower():
                    soup = BeautifulSoup(text, "html.parser")
                    for tag in soup(["script", "style", "noscript", "svg"]):
                        tag.decompose()
                    title = soup.title.get_text(strip=True) if soup.title else ""
                    body = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n"))
                    return f"{title}\n\n{body}".strip()[:MAX_CHARS]
                if "text" in ctype.lower() or "json" in ctype.lower() or "xml" in ctype.lower() or not ctype:
                    return text[:MAX_CHARS]
                return f"Contenido binario ({ctype}, {len(data)} bytes) no legible como texto."
    return "Demasiadas redirecciones."


# ------------------------------------------------------- descargas y caché
MAX_DOWNLOAD = 50 * 1024 * 1024
GITHUB_HOSTS = ("github.com", "api.github.com", "codeload.github.com")


def cache_put(cache, name, data):
    """Guarda los últimos archivos del canal para poder leerlos después con FILE."""
    cache.pop(name, None)
    cache[name] = data
    while len(cache) > 5 or sum(len(v) for v in cache.values()) > 80 * 1024 * 1024:
        cache.pop(next(iter(cache)))


async def _download(url):
    current = url
    for _ in range(5):
        if not await _is_safe(current):
            raise ValueError("URL bloqueada (no es http/https público).")
        headers = {"User-Agent": UA}
        if GITHUB_TOKEN and urlparse(current).hostname in GITHUB_HOSTS:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        async with aiohttp.ClientSession() as s:
            async with s.get(current, headers=headers, allow_redirects=False,
                             timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    current = urljoin(current, r.headers["Location"])
                    continue
                if r.status >= 400:
                    raise ValueError(f"HTTP {r.status}")
                if r.content_length and r.content_length > MAX_DOWNLOAD:
                    raise ValueError("el archivo pesa más de 25MB")
                buf = bytearray()
                async for chunk in r.content.iter_chunked(65536):
                    buf += chunk
                    if len(buf) > MAX_DOWNLOAD:
                        raise ValueError("el archivo pesa más de 25MB")
                cd = r.headers.get("Content-Disposition", "")
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
                name = m.group(1) if m else (os.path.basename(urlparse(current).path) or "descarga.bin")
                return bytes(buf), name
    raise ValueError("Demasiadas redirecciones.")


async def download_analyze(url, ctx, name_hint=None):
    data, name = await _download(url.strip().strip("<>"))
    name = name_hint or name
    if ctx and ctx.get("cache") is not None:
        cache_put(ctx["cache"], name, data)
    info = await asyncio.to_thread(filereader.analyze, name, data, 15000)
    return info + f"\n\n(Guardado como '{name}': puedes leer un archivo interno con FILE.)"


async def github_zip(arg, ctx):
    ref_arg = arg.strip().rstrip("/")
    ref_arg = re.sub(r"^https?://github\.com/", "", ref_arg)
    parts = ref_arg.split("/")
    if len(parts) < 2:
        return "Formato: usuario/repo, usuario/repo@rama o URL del repo."
    owner, repo, branch = parts[0], parts[1], None
    if repo.endswith(".git"):
        repo = repo[:-4]
    if "@" in repo:
        repo, branch = repo.split("@", 1)
    elif len(parts) >= 4 and parts[2] == "tree":
        branch = "/".join(parts[3:])
    ref = branch or "HEAD"
    return await download_analyze(f"https://github.com/{owner}/{repo}/archive/{ref}.zip", ctx,
                                  name_hint=f"{repo}.zip")


async def read_cached_file(arg, ctx):
    cache = (ctx or {}).get("cache") or {}
    if not cache:
        return "No hay archivos guardados en este canal. Pide al usuario que suba uno o usa DOWNLOAD/GITHUB_ZIP."
    name, _, member = arg.partition("::")
    name, member = name.strip(), member.strip()
    key = None
    if name:
        low = name.lower()
        key = next((k for k in cache if k.lower() == low), None) or next((k for k in cache if low in k.lower()), None)
    elif len(cache) == 1:
        key = next(iter(cache))
    if key is None:
        return "Archivos disponibles: " + ", ".join(cache) + ". Usa: nombre :: ruta/interna"
    if not member:
        return await asyncio.to_thread(filereader.analyze, key, cache[key], 15000)
    return await asyncio.to_thread(filereader.read_member, key, cache[key], member)


# ------------------------------------------------------- ejecutar código
def _limits():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))


async def run_code(lang, code, ctx):
    owners = {x.strip() for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()}
    if not owners or str((ctx or {}).get("author_id")) not in owners:
        return "La ejecución de código no está habilitada para este usuario."
    langs = {"python": ("py", [sys.executable, "-I", "{f}"]), "node": ("js", ["node", "{f}"]),
             "bash": ("sh", ["bash", "{f}"]),
             "c": ("c", ["bash", "-c", "gcc {f} -o m && ./m"]), "cpp": ("cpp", ["bash", "-c", "g++ {f} -o m && ./m"]),
             "go": ("go", ["go", "run", "{f}"]), "java": ("java", ["java", "{f}"]),
             "rust": ("rs", ["bash", "-c", "rustc {f} -o m && ./m"])}
    if lang not in langs:
        return f"Lenguaje no soportado: {lang}"
    ext, cmd = langs[lang]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, f"main.{ext}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        shell = "-c" in cmd  # los compilados corren con ruta relativa dentro del tmp
        cmd = [c.replace("{f}", f"main.{ext}" if shell else path) for c in cmd]
        env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "LANG": "C.UTF-8"}
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=tmp, env=env, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, preexec_fn=_limits, start_new_session=True)
        except FileNotFoundError:
            return f"No está instalado el intérprete de {lang}."
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
            status = f"exit={proc.returncode}"
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, status = b"", "TIMEOUT (20s)"
    return f"{status}\n" + out.decode("utf-8", "replace")[:4000]


# ------------------------------------------------------------ skills extra
import ast
import json as _json
import operator as _op
from datetime import datetime, timezone

_OPS = {ast.Add: _op.add, ast.Sub: _op.sub, ast.Mult: _op.mul, ast.Div: _op.truediv,
        ast.Pow: _op.pow, ast.Mod: _op.mod, ast.FloorDiv: _op.floordiv, ast.USub: _op.neg}


def _ev(n):
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
        return n.value
    if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
        a, b = _ev(n.left), _ev(n.right)
        if isinstance(n.op, ast.Pow) and abs(b) > 1000:
            raise ValueError("exponente enorme")
        return _OPS[type(n.op)](a, b)
    if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
        return _OPS[type(n.op)](_ev(n.operand))
    raise ValueError("expresión no permitida")


async def calc(expr):
    return str(_ev(ast.parse(expr.strip(), mode="eval").body))


async def _get_json(url, params=None, text=False):
    async with aiohttp.ClientSession(headers={"User-Agent": UA}) as s:
        async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return f"HTTP {r.status}"
            return (await r.text()) if text else await r.json(content_type=None)


async def wiki(query):
    d = await _get_json("https://es.wikipedia.org/api/rest_v1/page/summary/" + query.strip().replace(" ", "_"))
    if isinstance(d, dict) and d.get("extract"):
        return f"{d.get('title')}: {d['extract']}"[:MAX_CHARS]
    return await web_search(query + " wikipedia")


async def weather(place):
    return (await _get_json("https://wttr.in/" + place.strip().replace(" ", "+"),
                            params={"format": "%l: %C %t sensación %f humedad %h viento %w"}, text=True))[:500]


async def pypi(name):
    d = await _get_json(f"https://pypi.org/pypi/{name.strip()}/json")
    if not isinstance(d, dict):
        return str(d)
    i = d["info"]
    return f"{i['name']} {i['version']}: {i.get('summary')} | {i.get('home_page') or i.get('project_urls')}"[:1500]


async def npm(name):
    d = await _get_json(f"https://registry.npmjs.org/{name.strip()}/latest")
    if not isinstance(d, dict):
        return str(d)
    return f"{d.get('name')} {d.get('version')}: {d.get('description')}"[:1500]


async def webhook(arg, ctx):
    """[[WEBHOOK: url | payload]] -> POST. Solo dueños (puede escribir en servicios externos)."""
    owners = {x.strip() for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()}
    if str((ctx or {}).get("author_id")) not in owners:
        return "Solo los dueños del bot pueden enviar webhooks."
    url, _, payload = arg.partition("|")
    url, payload = url.strip().strip("<>"), payload.strip()
    method = "POST"
    m = re.match(r"(GET|POST|PUT|PATCH|DELETE)\s+(\S.*)", url, re.I)
    if m:
        method, url = m.group(1).upper(), m.group(2).strip()
    if not await _is_safe(url):
        return "URL no permitida."
    payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", payload)
    try:
        body = _json.loads(payload) if payload else None
    except ValueError:
        body = {"content": payload[:1900]} if "discord" in url else None
    last = ""
    async with aiohttp.ClientSession(headers={"User-Agent": UA}) as s:
        for attempt in range(3):
            kw = {}
            if body is not None:
                kw["json"] = body
            elif payload:
                kw["data"] = payload
            try:
                async with s.request(method, url, timeout=aiohttp.ClientTimeout(total=20),
                                     allow_redirects=False, **kw) as r:
                    txt = (await r.text())[:300]
                    if r.status == 429:
                        wait = min(float(r.headers.get("Retry-After", "2") or 2), 10)
                        await asyncio.sleep(wait)
                        last = f"HTTP 429: {txt}"
                        continue
                    return f"HTTP {r.status}: {txt}"
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last = f"Error de red: {e}"
                await asyncio.sleep(1 + attempt)
    return last or "Sin respuesta."


async def gh_api(arg, ctx):
    """[[GH_API: METHOD /ruta | json]] -> cualquier endpoint de la API de GitHub. Solo dueños."""
    owners = {x.strip() for x in os.getenv("OWNER_IDS", "").split(",") if x.strip()}
    if str((ctx or {}).get("author_id")) not in owners:
        return "Solo los dueños del bot pueden usar la API de GitHub con su token."
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return "Falta GITHUB_TOKEN en los Secrets."
    spec, _, payload = arg.partition("|")
    parts = spec.strip().split(None, 1)
    if len(parts) != 2 or parts[0].upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE") or not parts[1].startswith("/"):
        return "Formato: [[GH_API: GET /user/repos | {json opcional}]]"
    method, path = parts[0].upper(), parts[1].strip()
    if method == "DELETE" and not re.search(r"borra|elimina|delete|quita", (ctx or {}).get("prompt", ""), re.I):
        return "DELETE bloqueado: el dueño no pidió borrar nada en su mensaje."
    kw = {}
    if payload.strip():
        try:
            kw["json"] = _json.loads(payload)
        except ValueError:
            return "El JSON del payload no es válido."
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28", "User-Agent": UA}
    async with aiohttp.ClientSession(headers=hdr) as s:
        async with s.request(method, "https://api.github.com" + path, timeout=aiohttp.ClientTimeout(total=30), **kw) as r:
            return f"HTTP {r.status}: {(await r.text())[:3500]}"


async def crypto(sym):
    ids = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "doge": "dogecoin", "bnb": "binancecoin",
           "xrp": "ripple", "ada": "cardano", "ltc": "litecoin", "usdt": "tether"}
    s = sym.strip().lower()
    d = await _get_json("https://api.coingecko.com/api/v3/simple/price",
                        params={"ids": ids.get(s, s), "vs_currencies": "usd,mxn", "include_24hr_change": "true"})
    return _json.dumps(d)[:600] if isinstance(d, dict) else str(d)


async def stack(query):
    d = await _get_json("https://api.stackexchange.com/2.3/search/advanced",
                        params={"order": "desc", "sort": "relevance", "q": query.strip(), "site": "stackoverflow",
                                "pagesize": "5", "accepted": "True"})
    if not isinstance(d, dict):
        return str(d)
    items = d.get("items", [])
    return "\n".join(f"- {i['title']} (score {i['score']}) {i['link']}" for i in items) or "Sin resultados."


async def dns(host):
    host = host.strip().lower()
    if not re.fullmatch(r"[a-z0-9.-]{3,253}", host):
        return "Dominio inválido."
    out = []
    for typ in ("A", "AAAA", "MX", "TXT", "NS"):
        d = await _get_json("https://dns.google/resolve", params={"name": host, "type": typ})
        if isinstance(d, dict):
            for a in d.get("Answer", [])[:5]:
                out.append(f"{typ}: {a.get('data')}")
    return "\n".join(out) or "Sin registros."


async def ghfile(arg):
    """[[GHFILE: usuario/repo :: ruta]] -> contenido de un archivo de GitHub."""
    repo, _, path = arg.partition("::")
    repo, path = repo.strip(), path.strip().lstrip("/")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not path:
        return "Formato: [[GHFILE: usuario/repo :: ruta/archivo]]"
    async with aiohttp.ClientSession(headers=_gh_headers(raw=True)) as s:
        async with s.get(f"https://api.github.com/repos/{repo}/contents/{path}",
                         timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status != 200:
                return f"HTTP {r.status}"
            return (await r.text())[:MAX_CHARS]


async def now(_):
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")



async def translate(arg):
    """[[TRANSLATE: en :: texto]] -> traduce (idioma origen automático)."""
    lang, sep, text = arg.partition("::")
    if not sep:
        lang, text = "es", arg
    lang = lang.strip().lower()[:5] or "es"
    d = await _get_json("https://translate.googleapis.com/translate_a/single",
                        params={"client": "gtx", "sl": "auto", "tl": lang, "dt": "t", "q": text.strip()[:4000]})
    if isinstance(d, list) and d and d[0]:
        return "".join(p[0] for p in d[0] if p and p[0])[:MAX_CHARS]
    # Respaldo gratis si Google limita (HTTP 429)
    m = await _get_json("https://api.mymemory.translated.net/get",
                        params={"q": text.strip()[:500], "langpair": f"autodetect|{lang}"})
    if isinstance(m, dict) and (m.get("responseData") or {}).get("translatedText"):
        return m["responseData"]["translatedText"]
    return str(d)


async def currency(arg):
    """[[CURRENCY: 100 usd mxn]]"""
    m = re.match(r"\s*([\d.,]+)?\s*([a-zA-Z]{3})\s*(?:a|to|->)?\s*([a-zA-Z]{3})", arg)
    if not m:
        return "Formato: [[CURRENCY: 100 usd mxn]]"
    amt = float((m.group(1) or "1").replace(",", ""))
    src, dst = m.group(2).upper(), m.group(3).upper()
    d = await _get_json(f"https://open.er-api.com/v6/latest/{src}")
    if not isinstance(d, dict) or dst not in d.get("rates", {}):
        return f"No encontré la tasa {src}->{dst}."
    return f"{amt:g} {src} = {amt * d['rates'][dst]:,.2f} {dst} (actualizado {d.get('time_last_update_utc', '?')})"


async def ipinfo(ip):
    ip = ip.strip()
    if not re.fullmatch(r"[0-9a-fA-F:.]{3,45}", ip):
        return "IP inválida."
    d = await _get_json(f"https://ipwho.is/{ip}")
    if not isinstance(d, dict) or not d.get("success"):
        return f"Sin datos: {d}"[:300]
    c = d.get("connection", {})
    return f"{ip}: {d.get('city')}, {d.get('region')}, {d.get('country')} | ISP {c.get('isp')} | ASN {c.get('asn')}"


async def qr(text):
    from urllib.parse import quote
    return "QR listo (imagen): https://api.qrserver.com/v1/create-qr-code/?size=500x500&data=" + quote(text.strip()[:900])


async def shorten(url):
    url = url.strip().strip("<>")
    if not await _is_safe(url):
        return "URL no permitida."
    return (await _get_json("https://is.gd/create.php", params={"format": "simple", "url": url}, text=True))[:300]


async def define(word):
    d = await _get_json("https://api.dictionaryapi.dev/api/v2/entries/en/" + word.strip())
    if not isinstance(d, list):
        return await wiki(word)
    out = []
    for meaning in d[0].get("meanings", [])[:3]:
        defs = meaning.get("definitions", [])
        if defs:
            out.append(f"({meaning.get('partOfSpeech')}) {defs[0].get('definition')}")
    return f"{d[0].get('word')}: " + " | ".join(out)


async def site_status(url):
    import time as _t
    url = url.strip().strip("<>")
    if not url.startswith("http"):
        url = "https://" + url
    if not await _is_safe(url):
        return "URL no permitida."
    t0 = _t.time()
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=False) as r:
                return (f"{url} -> HTTP {r.status} en {(_t.time() - t0) * 1000:.0f} ms | "
                        f"server: {r.headers.get('Server', '?')} | tipo: {r.headers.get('Content-Type', '?')}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return f"{url} CAÍDO o sin respuesta: {e or type(e).__name__}"


async def ghuser(user):
    user = user.strip().lstrip("@")
    if not re.fullmatch(r"[\w-]{1,39}", user):
        return "Usuario inválido."
    async with aiohttp.ClientSession(headers=_gh_headers()) as s:
        async with s.get(f"https://api.github.com/users/{user}", timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return f"HTTP {r.status}"
            u = await r.json()
        async with s.get(f"https://api.github.com/users/{user}/repos",
                         params={"sort": "updated", "per_page": "8"}, timeout=aiohttp.ClientTimeout(total=20)) as r:
            repos = await r.json() if r.status == 200 else []
    lines = [f"{u.get('login')} ({u.get('name') or '-'}) | {u.get('public_repos')} repos | "
             f"{u.get('followers')} seguidores | {u.get('html_url')}"]
    lines += [f"- {x['name']} ★{x['stargazers_count']} {x.get('language') or ''}: {x.get('description') or ''}"[:200]
              for x in repos]
    return "\n".join(lines)


async def releases(repo):
    repo = repo.strip()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return "Formato: [[RELEASES: usuario/repo]]"
    async with aiohttp.ClientSession(headers=_gh_headers()) as s:
        async with s.get(f"https://api.github.com/repos/{repo}/releases", params={"per_page": "3"},
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return f"HTTP {r.status}"
            rel = await r.json()
    if not rel:
        return "Ese repo no tiene releases."
    out = []
    for x in rel:
        assets = ", ".join(f"{a['name']} {a['browser_download_url']}" for a in x.get("assets", [])[:4])
        out.append(f"{x.get('tag_name')} ({x.get('published_at', '')[:10]}): {(x.get('body') or '')[:300]} | {assets}")
    return "\n".join(out)[:MAX_CHARS]


async def news(query):
    q = query.strip()
    params = {"tags": "front_page"} if q.lower() in ("", "top", "hoy", "x") else {"query": q, "tags": "story"}
    d = await _get_json("https://hn.algolia.com/api/v1/search", params=params)
    if not isinstance(d, dict):
        return str(d)
    return "\n".join(f"- {h.get('title')} ({h.get('points')} pts) {h.get('url') or ''}"
                     for h in d.get("hits", [])[:8]) or "Sin noticias."


async def hashes(text):
    import hashlib
    b = text.encode()
    return (f"md5: {hashlib.md5(b).hexdigest()}\nsha1: {hashlib.sha1(b).hexdigest()}\n"
            f"sha256: {hashlib.sha256(b).hexdigest()}")


async def b64(arg):
    import base64 as _b
    mode, sep, text = arg.partition("::")
    if not sep:
        mode, text = "encode", arg
    text = text.strip()
    if mode.strip().lower().startswith("d"):
        try:
            return _b.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", "replace")[:MAX_CHARS]
        except ValueError as e:
            return f"Base64 inválido: {e}"
    return _b.b64encode(text.encode()).decode()


async def uuids(n):
    import uuid
    k = max(1, min(int(n) if str(n).strip().isdigit() else 1, 20))
    return "\n".join(str(uuid.uuid4()) for _ in range(k))


async def json_check(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return "JSON válido:\n" + _json.dumps(_json.loads(text), indent=2, ensure_ascii=False)[:MAX_CHARS]
    except ValueError as e:
        return f"JSON inválido: {e}"


SKILLS_EXTRA = {"TRANSLATE": translate, "CURRENCY": currency, "IP": ipinfo, "QR": qr, "SHORT": shorten,
                "DEFINE": define, "STATUS": site_status, "GHUSER": ghuser, "RELEASES": releases,
                "NEWS": news, "HASH": hashes, "B64": b64, "UUID": uuids, "JSON": json_check}

# ------------------------------------------------------------------ dispatch
async def run_tool(name, arg, ctx=None):
    try:
        if name == "SEARCH":
            return await web_search(arg)
        if name == "GITHUB":
            return await github_repos(arg)
        if name == "GITHUB_CODE":
            return await github_code(arg)
        if name == "GITHUB_ZIP":
            return await github_zip(arg, ctx)
        if name == "FETCH":
            return await fetch_url(arg)
        if name == "DOWNLOAD":
            return await download_analyze(arg, ctx)
        if name == "FILE":
            return await read_cached_file(arg, ctx)
        if name == "GH_API":
            return await gh_api(arg, ctx)
        if name == "WEBHOOK":
            return await webhook(arg, ctx)
        extra = {"CALC": calc, "WIKI": wiki, "WEATHER": weather, "PYPI": pypi, "NPM": npm, "TIME": now,
                 "CRYPTO": crypto, "STACK": stack, "DNS": dns, "GHFILE": ghfile, **SKILLS_EXTRA}
        if name in extra:
            return await extra[name](arg)
        if name.startswith("RUN:"):
            return await run_code(name[4:], arg, ctx)
        return "Herramienta desconocida."
    except Exception as e:  # nunca romper el flujo por una herramienta
        return f"Error en la herramienta {name}: {e}"
