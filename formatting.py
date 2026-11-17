"""Convierte la respuesta del modelo en texto + archivos .txt para Discord."""
import os
import posixpath
import re

RAW_FILES = os.getenv("RAW_FILES", "1") == "1"  # 1 = entrega con su extensión real

CODE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
HEADER_RE = re.compile(
    r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?(?:archivo|file)[ \t]*:[ \t]*`?([^\n`*]+?)`?(?:\*\*)?[ \t]*$"
)

EXT = {
    "python": "py", "py": "py", "javascript": "js", "js": "js", "node": "js",
    "typescript": "ts", "ts": "ts", "bash": "sh", "sh": "sh", "shell": "sh",
    "zsh": "sh", "html": "html", "css": "css", "json": "json", "lua": "lua",
    "luau": "lua", "java": "java", "kotlin": "kt", "kt": "kt", "c": "c",
    "cpp": "cpp", "c++": "cpp", "csharp": "cs", "c#": "cs", "cs": "cs",
    "go": "go", "golang": "go", "rust": "rs", "rs": "rs", "php": "php",
    "sql": "sql", "yaml": "yml", "yml": "yml", "ruby": "rb", "rb": "rb",
    "powershell": "ps1", "ps1": "ps1", "batch": "bat", "bat": "bat",
    "dockerfile": "dockerfile", "toml": "toml", "xml": "xml", "markdown": "md",
    "md": "md", "swift": "swift", "dart": "dart", "text": "txt", "txt": "txt",
}


def _filename(header, lang, n, used):
    if header:
        base = header.replace("\\", "/").strip().strip("/").replace("/", "_")
    else:
        ext = EXT.get(lang, lang or "txt")
        base = f"script_{n}.{ext}"
    base = re.sub(r"[^\w.\-]+", "_", base).strip("._") or f"script_{n}"
    if RAW_FILES:
        if "." not in base:
            base += "." + EXT.get(lang, lang or "txt")
    elif not base.lower().endswith(".txt"):
        base += ".txt"
    candidate, k = base, 2
    while candidate in used:
        candidate = f"{k}_{base}"
        k += 1
    used.add(candidate)
    return candidate


def _orig_path(header, default):
    if not header:
        return default
    parts = [re.sub(r"[^\w.\-]+", "_", p) for p in header.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(p for p in parts if p) or default


def build_zip(files):
    """files: [(nombre_txt, contenido, ruta_original)] -> bytes de un ZIP con las rutas reales."""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for _, content, path in files:
            z.writestr(path, content)
    return buf.getvalue()


def split_output(text):
    """Devuelve (texto_sin_archivos, [(nombre.txt, contenido, ruta_original), ...])."""
    if text.count("```") % 2 == 1:  # bloque cortado por límite de tokens
        text += "\n```"
    files, pieces, used = [], [], set()
    last, n = 0, 0
    for m in CODE_RE.finditer(text):
        before = text[last:m.start()]
        last = m.end()
        lang = m.group(1).strip().lower()
        lang = lang.split()[0] if lang else ""
        code = m.group(2)
        header = None
        hm = list(HEADER_RE.finditer(before))
        if hm and not before[hm[-1].end():].strip():
            header = hm[-1].group(1).strip()
            before = before[:hm[-1].start()]
        pieces.append(before)
        small = code.count("\n") < 3 and len(code) < 200
        if small and not header:
            pieces.append(m.group(0))  # comando corto: se queda en el chat
            continue
        n += 1
        name = _filename(header, lang, n, used)
        files.append((name, code, _orig_path(header, name[:-4] if name.endswith(".txt") else name)))
    pieces.append(text[last:])
    clean = re.sub(r"\n{3,}", "\n\n", "".join(pieces)).strip()
    return clean, files


def chunk_text(text, limit=1900):
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]
