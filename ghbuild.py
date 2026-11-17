"""GitHub: compila APK / EXE con Actions y publica webs con Pages. Usa GITHUB_TOKEN (scopes repo + workflow)."""
import asyncio
import base64
import io
import json
import os
import re
import time
import zipfile

import aiohttp

API = "https://api.github.com"


def _token():
    return os.getenv("GITHUB_TOKEN", "")


APK_WF = """name: apk
on: workflow_dispatch
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: 17
      - uses: gradle/actions/setup-gradle@v4
        with:
          gradle-version: 8.7
      - run: gradle assembleDebug --no-daemon
      - uses: actions/upload-artifact@v4
        with:
          name: out
          path: "**/build/outputs/apk/**/*.apk"
"""


APK_TEMPLATE = {
    "settings.gradle": """pluginManagement {
    repositories { google(); mavenCentral(); gradlePluginPortal() }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories { google(); mavenCentral() }
}
rootProject.name = "DolphinApp"
include ':app'
""",
    "build.gradle": """plugins {
    id 'com.android.application' version '8.5.2' apply false
    id 'org.jetbrains.kotlin.android' version '1.9.24' apply false
}
""",
    "gradle.properties": "org.gradle.jvmargs=-Xmx3g\nandroid.useAndroidX=true\nandroid.nonTransitiveRClass=true\n",
    "app/build.gradle": """plugins {
    id 'com.android.application'
    id 'org.jetbrains.kotlin.android'
}
android {
    namespace 'com.dolphin.app'
    compileSdk 34
    defaultConfig {
        applicationId 'com.dolphin.app'
        minSdk 24
        targetSdk 34
        versionCode 1
        versionName '1.0'
    }
    compileOptions {
        sourceCompatibility JavaVersion.VERSION_17
        targetCompatibility JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = '17' }
    lint { abortOnError false }
}
dependencies {
    implementation 'androidx.core:core-ktx:1.13.1'
    implementation 'androidx.appcompat:appcompat:1.7.0'
    implementation 'androidx.constraintlayout:constraintlayout:2.1.4'
}
""",
    "app/src/main/AndroidManifest.xml": """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.INTERNET" />
    <application
        android:label="Dolphin App"
        android:usesCleartextTraffic="true"
        android:theme="@style/Theme.AppCompat.Light.NoActionBar">
        <activity android:name=".MainActivity" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
""",
    "app/src/main/java/com/dolphin/app/MainActivity.kt": """package com.dolphin.app

import android.annotation.SuppressLint
import android.os.Bundle
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {
    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val web = WebView(this)
        setContentView(web)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.webViewClient = WebViewClient()
        web.webChromeClient = WebChromeClient()
        web.loadUrl("file:///android_asset/index.html")
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else finish()
            }
        })
    }
}
""",
    "app/src/main/assets/index.html": "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><h1>Dolphin App</h1>",
}
# El modelo solo puede aportar estas rutas; build.gradle, settings, manifest y gradle.properties son fijos.
APK_ALLOWED = re.compile(r"^app/src/main/(java|kotlin|assets|res)/")


def apk_project(files):
    out = dict(APK_TEMPLATE)
    files = norm_files(files)
    todo = [(p, c) for _, c, p in files]
    idx = pick_index([(p, c) for p, c in todo if "/" not in p or p.startswith("app/src/main/assets/")])
    if idx:
        out["app/src/main/assets/index.html"] = idx[1]       # portada = index.html o el html más grande
    for path, content in todo:
        if APK_ALLOWED.match(path) and not path.endswith("assets/index.html"):
            out[path] = content
        elif "/" not in path and path.lower().endswith((".css", ".js")):
            out[f"app/src/main/assets/{path}"] = content      # recursos web sueltos
        elif path.endswith(".kt") and "MainActivity" in path:
            out["app/src/main/java/com/dolphin/app/MainActivity.kt"] = content
    return out


def exe_workflow(paths):
    names = {p.lower() for p in paths}
    head = "name: exe\non: workflow_dispatch\njobs:\n  build:\n    runs-on: windows-latest\n    steps:\n      - uses: actions/checkout@v4\n"
    tail = "      - uses: actions/upload-artifact@v4\n        with:\n          name: out\n          path: {path}\n"
    if any(n.endswith(".csproj") for n in names):
        body = ("      - uses: actions/setup-dotnet@v4\n        with:\n          dotnet-version: 8.0.x\n"
                "      - run: dotnet publish -c Release -r win-x64 --self-contained -p:PublishSingleFile=true -o out\n")
        return head + body + tail.format(path="out/*.exe")
    if "go.mod" in names:
        body = ("      - uses: actions/setup-go@v5\n        with:\n          go-version: stable\n"
                "      - run: go build -o app.exe .\n")
        return head + body + tail.format(path="app.exe")
    if "cargo.toml" in names:
        return head + "      - run: cargo build --release\n" + tail.format(path="target/release/*.exe")
    py = [p for p in paths if p.endswith(".py")]
    if py:
        main = next((p for p in py if os.path.basename(p) in ("main.py", "app.py", "bot.py")), py[0])
        body = ("      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'\n"
                "      - run: |\n          pip install pyinstaller\n"
                "          if (Test-Path requirements.txt) { pip install -r requirements.txt }\n"
                f"          pyinstaller --onefile {main}\n")
        return head + body + tail.format(path="dist/*.exe")
    js = [p for p in paths if p.endswith(".js")]
    if js:
        main = next((p for p in js if os.path.basename(p) in ("index.js", "main.js", "app.js")), js[0])
        body = ("      - uses: actions/setup-node@v4\n        with:\n          node-version: 20\n"
                "      - run: |\n          if (Test-Path package.json) { npm install }\n"
                f"          npx --yes @yao-pkg/pkg {main} --targets node20-win-x64 -o app.exe\n")
        return head + body + tail.format(path="app.exe")
    return None


def norm_files(files):
    """[(nombre, contenido, ruta)] -> rutas limpias: sin .txt sobrante y con html detectado por contenido."""
    out = []
    for name, content, path in files:
        path = path.lstrip("./")
        base = path.rsplit("/", 1)[-1]
        m = re.match(r"^(.+\.(?:html?|css|js|json|py|kt|java|go|rs|cs|md|xml|ya?ml))\.txt$", base, re.I)
        if m:
            path = path[: -len(base)] + m.group(1)
        if re.match(r"^script_\d+\.txt$", path.rsplit("/", 1)[-1]) and re.search(r"<!doctype html|<html", content[:400], re.I):
            path = path[:-4] + ".html"
        out.append((path.rsplit("/", 1)[-1], content, path))
    return out


def pick_index(todo):
    """Devuelve (ruta, contenido) del html que será la portada, o None."""
    htmls = [(p, c) for p, c in todo if p.lower().endswith((".html", ".htm"))]
    if not htmls:
        return None
    for p, c in htmls:
        if p == "index.html":
            return p, c
    for p, c in htmls:
        if p.endswith("/index.html"):
            return p, c
    return max(htmls, key=lambda x: len(x[1]))


async def _fail_logs(call, full, run_id):
    """Cola del log del job que falló (líneas con error) para ver por qué no compiló."""
    try:
        st, body = await call("GET", f"{API}/repos/{full}/actions/runs/{run_id}/jobs")
        jobs = json.loads(body).get("jobs", []) if st == 200 else []
        if not jobs:
            return ""
        st, log = await call("GET", f"{API}/repos/{full}/actions/jobs/{jobs[0]['id']}/logs")
        if st != 200:
            return ""
        lines = [l.split("Z ", 1)[-1] for l in log.decode("utf-8", "replace").splitlines()]
        errs = [l for l in lines if re.search(r"error|FAILED|What went wrong|e: |Exception", l, re.I)]
        return "\n".join((errs or lines)[-12:])[-1200:]
    except Exception:
        return ""


async def build(kind, files):
    """kind: apk | exe | pages. files: [(nombre, contenido, ruta)]. -> (bytes|None, nombre_archivo, mensaje)"""
    if not _token():
        return None, "", "Falta GITHUB_TOKEN (scopes repo + workflow) en los Secrets."
    if kind == "apk":
        proj = apk_project(files)
        files = [(p.rsplit("/", 1)[-1], c, p) for p, c in proj.items()]
    paths = [p for _, _, p in files]
    if kind == "apk":
        wf, wf_name, ext = APK_WF, "apk.yml", ".apk"
    elif kind == "exe":
        wf, wf_name, ext = exe_workflow(paths), "exe.yml", ".exe"
        if not wf:
            return None, "", "No encontré proyecto compilable a .exe (Python, Node, Go, Rust o .NET)."
    else:
        wf, wf_name, ext = None, "", ""
    hdr = {"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dolphin-bot"}
    name = f"{kind}-{int(time.time())}"
    async with aiohttp.ClientSession(headers=hdr) as s:
        async def call(method, url, **kw):
            async with s.request(method, url, timeout=aiohttp.ClientTimeout(total=90), **kw) as r:
                return r.status, await r.read()

        st, body = await call("POST", f"{API}/user/repos",
                              json={"name": name, "private": kind != "pages", "auto_init": True})
        if st != 201:
            return None, "", f"No pude crear el repo: HTTP {st} {body[:200]!r}"
        repo = json.loads(body)
        full, branch, url = repo["full_name"], repo["default_branch"], repo["html_url"]
        todo = [(p, c) for _, c, p in norm_files(files) if not p.startswith(".github/")]
        if kind == "pages":
            idx = pick_index(todo)
            if not idx:
                return None, "", "No hay ningún .html para publicar. Pídeme la web completa en un index.html."
            if idx[0] != "index.html":
                todo = [(p, c) for p, c in todo if p != "index.html"] + [("index.html", idx[1])]
            todo = [(p, c) for p, c in todo if p.lower() != "readme.md"] + [(".nojekyll", "")]
        if wf:
            todo.append((f".github/workflows/{wf_name}", wf))
        for path, content in todo:
            st, body = await call("PUT", f"{API}/repos/{full}/contents/{path}", json={
                "message": f"add {path}", "branch": branch,
                "content": base64.b64encode(content.encode()).decode()})
            if st not in (200, 201):
                return None, "", f"Falló subir {path}: HTTP {st} {body[:150]!r} ({url})"

        if kind == "pages":
            st, body = await call("POST", f"{API}/repos/{full}/pages",
                                  json={"source": {"branch": branch, "path": "/"}})
            if st not in (201, 409):
                return None, "", f"No pude activar Pages: HTTP {st} {body[:150]!r} ({url})"
            owner, repo_name = full.split("/")
            site = f"https://{owner}.github.io/{repo_name}/"
            for _ in range(16):  # espera a que Pages termine de construir (~4 min máx)
                await asyncio.sleep(15)
                st, body = await call("GET", f"{API}/repos/{full}/pages/builds/latest")
                if st == 200:
                    status = json.loads(body).get("status")
                    if status == "built":
                        try:
                            async with s.get(site, timeout=aiohttp.ClientTimeout(total=20)) as r:
                                page = await r.text()
                                if r.status != 200:
                                    return None, "", f"Pages construyó pero {site} da HTTP {r.status}. Repo: {url}"
                                if 'content="Jekyll' in page and "<body" not in page.lower()[:3000].replace("<body>", "<body"):
                                    return None, "", f"Quedó la plantilla vacía de GitHub en {site}. Repo: {url}"
                        except Exception:
                            pass
                        return None, "", f"Web lista: {site}  | repo: {url}"
                    if status == "errored":
                        return None, "", f"Pages dio error al construir. Revisa {url}/settings/pages"
            return None, "", f"Web publicada pero aún construyendo: {site}  | repo: {url}"

        await asyncio.sleep(3)
        st, body = await call("POST", f"{API}/repos/{full}/actions/workflows/{wf_name}/dispatches", json={"ref": branch})
        if st != 204:
            return None, "", f"No pude lanzar el build: HTTP {st} {body[:150]!r} ({url})"
        run_id, deadline = None, time.time() + 1200
        while time.time() < deadline:
            await asyncio.sleep(15)
            st, body = await call("GET", f"{API}/repos/{full}/actions/runs")
            runs = json.loads(body).get("workflow_runs", []) if st == 200 else []
            if runs:
                run = runs[0]
                run_id = run["id"]
                if run["status"] == "completed":
                    if run["conclusion"] != "success":
                        tail = await _fail_logs(call, full, run_id)
                        return None, "", f"El build falló ({run['conclusion']}). Logs: {run['html_url']}\n```\n{tail}\n```"
                    break
        else:
            return None, "", f"El build tardó demasiado. Revísalo en {url}/actions"
        st, body = await call("GET", f"{API}/repos/{full}/actions/runs/{run_id}/artifacts")
        arts = json.loads(body).get("artifacts", []) if st == 200 else []
        if not arts:
            return None, "", f"Compiló pero no hay {ext} (revisa la ruta de salida): {url}/actions"
        st, data = await call("GET", arts[0]["archive_download_url"])
        if st != 200:
            return None, "", f"No pude bajar el artefacto: HTTP {st}. Está en {url}/actions"
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            outs = [n for n in z.namelist() if n.lower().endswith(ext)]
            if not outs:
                return None, "", f"El artefacto no trae {ext}: {url}/actions"
            return z.read(outs[0]), os.path.basename(outs[0]), f"Listo. Repo privado: {url}"


async def push(files, name=None, private=True):
    """Sube archivos a un repo (lo crea si no existe; si existe actualiza). -> mensaje"""
    if not _token():
        return "Falta GITHUB_TOKEN (scopes repo + workflow) en los Secrets."
    hdr = {"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dolphin-bot"}
    name = re.sub(r"[^\w.\-]+", "-", name or "").strip("-.") or f"proyecto-{int(time.time())}"
    async with aiohttp.ClientSession(headers=hdr) as s:
        async def call(method, url, **kw):
            async with s.request(method, url, timeout=aiohttp.ClientTimeout(total=90), **kw) as r:
                return r.status, await r.read()
        st, body = await call("GET", f"{API}/user")
        if st != 200:
            return f"Token inválido o sin permisos: HTTP {st}"
        login = json.loads(body)["login"]
        st, body = await call("GET", f"{API}/repos/{login}/{name}")
        if st == 404:
            st, body = await call("POST", f"{API}/user/repos", json={"name": name, "private": private, "auto_init": True})
            if st != 201:
                return f"No pude crear el repo: HTTP {st} {body[:200]!r}"
        elif st != 200:
            return f"No pude leer el repo: HTTP {st}"
        repo = json.loads(body)
        full, branch, url = repo["full_name"], repo["default_branch"], repo["html_url"]
        done = 0
        for _, content, path in norm_files(files):
            st, body = await call("GET", f"{API}/repos/{full}/contents/{path}?ref={branch}")
            payload = {"message": f"update {path}", "branch": branch,
                       "content": base64.b64encode(content.encode()).decode()}
            if st == 200:
                payload["sha"] = json.loads(body)["sha"]
            st, body = await call("PUT", f"{API}/repos/{full}/contents/{path}", json=payload)
            if st not in (200, 201):
                return f"Falló subir {path}: HTTP {st} {body[:150]!r} ({url})"
            done += 1
        return f"Subidos {done} archivo(s) a {url} ({'privado' if repo.get('private') else 'público'})"
