"""Lectura de archivos adjuntos: ZIP, APK, TAR/GZ/BZ2/XZ, 7z, RAR, PDF, DOCX, XLSX, PPTX, texto y binarios.

Todo se lee en memoria (nunca se extrae a disco) con límites contra zip-bombs.
"""
import bz2
import gzip
import io
import lzma
import posixpath
import re
import tarfile
import zipfile

MAX_READ = 200_000
PER_FILE_CHARS = 6000
MAX_UNCOMPRESSED = 300 * 1024 * 1024
MAX_LIST = 300
MAX_CONTENT_FILES = 40
MAX_NESTED = 3

TEXT_EXTS = {
    ".txt", ".md", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".yml", ".yaml",
    ".toml", ".ini", ".cfg", ".conf", ".env", ".html", ".htm", ".css", ".scss", ".xml", ".csv",
    ".tsv", ".sh", ".bat", ".ps1", ".lua", ".luau", ".java", ".kt", ".kts", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".go", ".rs", ".php", ".rb", ".sql", ".gradle", ".properties", ".lock",
    ".gitignore", ".dockerfile", ".log", ".smali", ".vue", ".svelte", ".r", ".swift", ".dart",
    ".mod", ".sum", ".bundle", ".nix", ".replit",
}
IMPORTANT = ("readme", "package.json", "requirements", "pyproject", "main", "index", "app",
             "manifest", "build.gradle", "dockerfile", ".replit", "config", "setup", "server", "bot")
ARCHIVE_EXTS = (".zip", ".jar", ".apk", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".aar", ".war")
TAR_EXTS = (".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz", ".tbz2", ".txz")
MAGICS = [
    (b"\x7fELF", "ejecutable ELF (Linux/Android nativo)"), (b"MZ", "ejecutable Windows PE (exe/dll)"),
    (b"SQLite format 3", "base de datos SQLite"), (b"\xca\xfe\xba\xbe", "Java .class / Mach-O fat"),
    (b"dex\n", "Dalvik DEX (Android)"), (b"OggS", "audio/video Ogg"), (b"ID3", "audio MP3"),
    (b"\x00\x00\x00\x18ftyp", "video/audio MP4"), (b"\x00\x00\x00\x20ftyp", "video/audio MP4"),
    (b"fLaC", "audio FLAC"), (b"RIFF", "contenedor RIFF (wav/avi/webp)"),
]


# ------------------------------------------------------------------ helpers
class Out:
    def __init__(self, budget):
        self.budget, self.used, self.parts = budget, 0, []

    def add(self, s):
        if self.used >= self.budget:
            return False
        room = self.budget - self.used
        if len(s) > room:
            s = s[:room] + "\n...[cortado por límite de tamaño]"
        self.parts.append(s)
        self.used += len(s)
        return True

    def text(self):
        return "\n".join(self.parts)


def _ext(name):
    return posixpath.splitext(name.lower())[1]


def _fmt(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def looks_text(data):
    if not data:
        return True
    s = data[:4096]
    if b"\x00" in s:
        return False
    ctrl = sum(1 for b in s if b < 9 or 13 < b < 32)
    return ctrl / len(s) < 0.05


def decode(data):
    text = data.decode("utf-8", errors="replace")
    if text.count("\ufffd") > max(3, len(text) // 100):
        return data.decode("latin-1", errors="replace")
    return text


def _strings(data, n=80, minlen=6):
    seen, res = set(), []
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % minlen, data[:3_000_000]):
        s = m.group().decode("ascii")
        if s not in seen:
            seen.add(s)
            res.append(s)
            if len(res) >= n:
                break
    return res


# ----------------------------------------------------------- member listing
def _zread(zf, info):
    def read(n):
        with zf.open(info) as f:
            return f.read(n)
    return read


def _zip_members(zf):
    return [{"path": i.filename, "size": i.file_size, "encrypted": bool(i.flag_bits & 1),
             "read": _zread(zf, i)} for i in zf.infolist()[:20000]]


def _tread(tf, m):
    def read(n):
        f = tf.extractfile(m)
        return f.read(n) if f else b""
    return read


def _tar_members(tf):
    ms, total = [], 0
    for k, m in enumerate(tf):
        if k >= 20000:
            break
        if not m.isfile():
            continue
        total += m.size
        ms.append({"path": m.name, "size": m.size, "read": _tread(tf, m)})
        if total > MAX_UNCOMPRESSED:
            break
    return ms


def _emit_members(members, out, depth, label, prefix_filter=None):
    total = sum(m["size"] for m in members)
    out.add(f"{label}: {len(members)} entradas, {_fmt(total)} descomprimido")
    listing = [f'{m["path"]} ({_fmt(m["size"])})' for m in members[:MAX_LIST]]
    extra = f"\n... y {len(members) - MAX_LIST} más" if len(members) > MAX_LIST else ""
    out.add("Listado:\n" + "\n".join(listing) + extra)
    if total > MAX_UNCOMPRESSED:
        out.add("Contenido NO leído: tamaño descomprimido sospechoso (posible zip bomb).")
        return

    cands = [m for m in members if not m["path"].endswith("/")
             and (not prefix_filter or m["path"].startswith(prefix_filter))]

    def prio(m):
        base = posixpath.basename(m["path"]).lower()
        return (0 if any(k in base for k in IMPORTANT) else 1, m["size"])

    shown = nested = 0
    skipped_text = 0
    for m in sorted(cands, key=prio):
        if out.used >= out.budget:
            break
        if m.get("encrypted"):
            out.add(f'\n--- {m["path"]} --- (cifrado, no se puede leer sin contraseña)')
            continue
        ext = _ext(m["path"])
        if ext in ARCHIVE_EXTS and depth < 2 and m["size"] < 20 * 1024 * 1024 and nested < MAX_NESTED:
            nested += 1
            try:
                raw = m["read"](20 * 1024 * 1024)
                sub = Out(min(4000, max(0, out.budget - out.used)))
                _dispatch(m["path"], raw, sub, depth + 1)
                out.add(f'\n=== archivo anidado {m["path"]} ===\n' + sub.text())
            except Exception as e:
                out.add(f'\n=== {m["path"]} === error leyendo anidado: {e}')
            continue
        if not (ext in TEXT_EXTS or ext == "") or m["size"] > 2 * 1024 * 1024:
            continue
        if shown >= MAX_CONTENT_FILES:
            skipped_text += 1
            continue
        try:
            raw = m["read"](MAX_READ)
        except Exception as e:
            out.add(f'\n--- {m["path"]} --- error: {e}')
            continue
        if not looks_text(raw):
            continue
        content = decode(raw)
        cut = " [...]" if len(content) > PER_FILE_CHARS else ""
        out.add(f'\n--- {m["path"]} ---\n{content[:PER_FILE_CHARS]}{cut}')
        shown += 1
    if skipped_text:
        out.add(f"\n(Hay {skipped_text} archivos de texto más sin mostrar por límite.)")


# --------------------------------------------------------------------- APK
def _apk_manifest(data, zf, out):
    try:
        from androguard.core.apk import APK
        a = APK(data, raw=True)
        lines = [
            f"Paquete: {a.get_package()}",
            f"Versión: {a.get_androidversion_name()} (code {a.get_androidversion_code()})",
            f"minSdk: {a.get_min_sdk_version()} / targetSdk: {a.get_target_sdk_version()}",
            f"Actividad principal: {a.get_main_activity()}",
            "Permisos: " + ", ".join(sorted(a.get_permissions())),
            "Actividades: " + ", ".join(list(a.get_activities())[:15]),
            "Servicios: " + ", ".join(list(a.get_services())[:10]),
            "Receivers: " + ", ".join(list(a.get_receivers())[:10]),
        ]
        out.add("Manifest (androguard):\n" + "\n".join(lines))
        return
    except ImportError:
        pass
    except Exception as e:
        out.add(f"(androguard falló: {e}; uso análisis aproximado)")
    try:
        raw = zf.read("AndroidManifest.xml")
    except KeyError:
        out.add("Sin AndroidManifest.xml.")
        return
    cand = {s.decode("utf-16le", "ignore") for s in re.findall(rb"(?:[\x20-\x7e]\x00){4,}", raw)}
    cand |= {s.decode("ascii") for s in re.findall(rb"[A-Za-z0-9_.]{6,}", raw)}
    perms = sorted(s for s in cand if "permission" in s.lower() and "." in s)
    others = sorted(s for s in cand if "." in s and " " not in s and s not in perms)[:60]
    out.add("Manifest (binario, extracción aproximada sin androguard):\n"
            "Permisos: " + ", ".join(perms) + "\nOtros strings: " + ", ".join(others))


def _analyze_apk(data, out, depth):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        dex = [i for i in infos if re.fullmatch(r"classes\d*\.dex", i.filename)]
        abis = sorted({n.split("/")[1] for n in names if n.startswith("lib/") and n.count("/") >= 2})
        out.add(f"APK de Android: {len(dex)} dex, ABIs nativas: {', '.join(abis) or 'ninguna'}, "
                f"{sum(1 for n in names if n.startswith('assets/'))} assets, "
                f"{sum(1 for n in names if n.startswith('res/'))} recursos.")
        _apk_manifest(data, zf, out)
        urls = set()
        for i in dex[:6]:
            if i.file_size > 60 * 1024 * 1024:
                continue
            with zf.open(i) as f:
                blob = f.read(25 * 1024 * 1024)
            for m in re.finditer(rb"https?://[\x21-\x7e]{6,150}", blob):
                urls.add(m.group().decode("ascii", "ignore"))
                if len(urls) >= 60:
                    break
        if urls:
            out.add("URLs encontradas en el código (dex):\n" + "\n".join(sorted(urls)))
        _emit_members(_zip_members(zf), out, depth, "Contenido del APK", prefix_filter="assets/")


# ------------------------------------------------------------ doc formats
def _analyze_pdf(data, out):
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    out.add(f"PDF de {len(r.pages)} páginas.")
    text = []
    for p in r.pages[:30]:
        text.append(p.extract_text() or "")
    joined = "\n\n".join(text).strip()
    out.add(joined if joined else "(sin texto extraíble: probablemente escaneado)")


def _analyze_office(ext, data, out):
    if ext == ".docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        text = "\n".join(p.text for p in d.paragraphs)
        for t in d.tables:
            for row in t.rows:
                text += "\n" + " | ".join(c.text for c in row.cells)
        out.add("Documento Word:\n" + text)
    elif ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for ws in wb.worksheets[:10]:
            rows = []
            for k, row in enumerate(ws.iter_rows(values_only=True)):
                if k >= 60:
                    rows.append("...")
                    break
                rows.append(" | ".join("" if c is None else str(c) for c in row))
            out.add(f"Hoja '{ws.title}':\n" + "\n".join(rows))
    elif ext == ".pptx":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            slides = sorted((n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                            key=lambda n: int(re.findall(r"\d+", n)[0]))
            for n in slides[:40]:
                txt = " ".join(re.findall(r"<a:t>(.*?)</a:t>", zf.read(n).decode("utf-8", "ignore")))
                out.add(f"Diapositiva {re.findall(r'\d+', n)[0]}: {txt}")


def _analyze_image(name, data, out):
    info = ""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        info = f" {im.format} {im.width}x{im.height} modo {im.mode}."
    except Exception:
        pass
    out.add(f"Imagen.{info} (No puedo ver los píxeles, solo metadatos.)")


# ------------------------------------------------------------- compressed
def _single_stream(data, out, name, depth):
    if data[:2] == b"\x1f\x8b":
        opener, inner = (lambda b: gzip.GzipFile(fileobj=b)), name[:-3] if name.lower().endswith(".gz") else name + ".out"
    elif data[:3] == b"BZh":
        opener, inner = bz2.BZ2File, name[:-4] if name.lower().endswith(".bz2") else name + ".out"
    else:
        opener, inner = lzma.LZMAFile, name[:-3] if name.lower().endswith(".xz") else name + ".out"
    limit = 50 * 1024 * 1024
    raw = opener(io.BytesIO(data)).read(limit + 1)
    if len(raw) > limit:
        out.add("Aviso: contenido descomprimido > 50MB, analizo solo el inicio.")
        raw = raw[:limit]
    out.add(f"Archivo comprimido de un solo flujo; contenido interno: {inner}")
    _dispatch(inner, raw, out, depth + 1)


def _try_tar(name, data, out, depth):
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.ReadError:
        return False
    with tf:
        _emit_members(_tar_members(tf), out, depth, "Archivo TAR")
    return True


def _analyze_7z(data, out, depth):
    import py7zr
    with py7zr.SevenZipFile(io.BytesIO(data)) as z:
        infos = z.list()
        total = sum(i.uncompressed or 0 for i in infos)
        files = [i for i in infos if not i.is_directory]
        members = [{"path": i.filename, "size": i.uncompressed or 0, "read": None} for i in files]
        if total > MAX_UNCOMPRESSED:
            _emit_members(members, out, depth, "Archivo 7z", prefix_filter="\0")
            return
        contents = z.readall()
    for m in members:
        blob = contents.get(m["path"])
        m["read"] = (lambda n, b=blob: b.read()[:n] if b is not None else b"")
    _emit_members(members, out, depth, "Archivo 7z")


def _analyze_rar(data, out, depth):
    import rarfile
    rf = rarfile.RarFile(io.BytesIO(data))
    members = []
    for i in rf.infolist()[:20000]:
        if i.is_dir():
            continue
        members.append({"path": i.filename, "size": i.file_size, "encrypted": i.needs_password(),
                        "read": (lambda n, i=i: rf.read(i)[:n])})
    _emit_members(members, out, depth, "Archivo RAR")


# ---------------------------------------------------------------- dispatch
def _dispatch(name, data, out, depth=0):
    ext = _ext(name)
    out.add(f"Archivo: {name} ({_fmt(len(data))})")

    if data[:5] == b"%PDF-":
        try:
            return _analyze_pdf(data, out)
        except Exception as e:
            out.add(f"(no pude leer el PDF: {e})")
    if data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[:4] == b"GIF8" \
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return _analyze_image(name, data, out)

    if zipfile.is_zipfile(io.BytesIO(data)):
        if ext == ".apk":
            try:
                return _analyze_apk(data, out, depth)
            except Exception as e:
                out.add(f"(análisis APK falló: {e}; sigo como ZIP)")
        if ext in (".docx", ".xlsx", ".pptx"):
            try:
                return _analyze_office(ext, data, out)
            except Exception as e:
                out.add(f"(no pude leer el documento Office: {e}; sigo como ZIP)")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return _emit_members(_zip_members(zf), out, depth, "Archivo ZIP/JAR")

    if data[:6] == b"7z\xbc\xaf\x27\x1c":
        try:
            return _analyze_7z(data, out, depth)
        except ImportError:
            return out.add("Es un .7z pero falta la librería py7zr (pip install py7zr).")
        except Exception as e:
            return out.add(f"No pude leer el 7z: {e}")
    if data[:4] == b"Rar!":
        try:
            return _analyze_rar(data, out, depth)
        except ImportError:
            return out.add("Es un .rar pero falta la librería rarfile (pip install rarfile).")
        except Exception as e:
            return out.add(f"No pude leer el RAR (¿falta 'unrar' o tiene contraseña?): {e}")

    compressed = data[:2] == b"\x1f\x8b" or data[:3] == b"BZh" or data[:6] == b"\xfd7zXZ\x00"
    if compressed or data[257:262] == b"ustar" or name.lower().endswith(TAR_EXTS):
        if _try_tar(name, data, out, depth):
            return
        if compressed:
            return _single_stream(data, out, name, depth)

    if looks_text(data):
        return out.add("Contenido:\n" + decode(data[: out.budget * 4 + 1000]))

    kind = next((desc for magic, desc in MAGICS if data.startswith(magic)), "binario desconocido")
    out.add(f"Binario: {kind}.")
    out.add("Primeros bytes (hex): " + data[:48].hex(" "))
    strs = _strings(data)
    if strs:
        out.add("Strings legibles:\n" + "\n".join(strs))


def analyze(name, data, budget=20000):
    out = Out(budget)
    try:
        _dispatch(name, data, out, 0)
    except Exception as e:
        out.add(f"Error leyendo el archivo: {e}")
    return out.text()


def _find_member(names, member):
    member = member.strip().lstrip("/")
    if member in names:
        return member
    low = member.lower()
    for n in names:
        if n.lower() == low or n.lower().endswith("/" + low):
            return n
    for n in names:
        if low in n.lower():
            return n
    return None


def _render_member(path, raw, limit):
    if looks_text(raw):
        text = decode(raw)
        cut = "\n[...cortado]" if len(text) > limit else ""
        return f"--- {path} ---\n{text[:limit]}{cut}"
    strs = _strings(raw, n=150)
    return (f"--- {path} --- (binario, {_fmt(len(raw))} leídos)\nHex: {raw[:64].hex(' ')}\n"
            "Strings:\n" + "\n".join(strs))


def read_member(name, data, member, limit=30000):
    """Lee un archivo concreto dentro de un ZIP/APK/JAR/TAR en memoria."""
    try:
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [i.filename for i in zf.infolist() if not i.filename.endswith("/")]
                match = _find_member(names, member)
                if not match:
                    return "No encontré ese archivo. Algunos del paquete:\n" + "\n".join(names[:40])
                info = zf.getinfo(match)
                if info.flag_bits & 1:
                    return f"{match} está cifrado."
                with zf.open(info) as f:
                    raw = f.read(limit * 4)
            return _render_member(match, raw, limit)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            names = []
            for k, m in enumerate(tf):
                if k >= 20000:
                    break
                if m.isfile():
                    names.append(m.name)
            match = _find_member(names, member)
            if not match:
                return "No encontré ese archivo. Algunos del paquete:\n" + "\n".join(names[:40])
            f = tf.extractfile(match)
            raw = f.read(limit * 4) if f else b""
        return _render_member(match, raw, limit)
    except Exception as e:
        return f"No pude leer ese archivo dentro de {name}: {e} (solo ZIP/APK/JAR/TAR)"
