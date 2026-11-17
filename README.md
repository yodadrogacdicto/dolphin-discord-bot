# Dolphin – Bot de Discord (solo modelos 100% gratis)

Usa **failover automático** entre modelos gratuitos (si uno falla, pasa al siguiente):
1. **OpenRouter** `cognitivecomputations/dolphin-mistral-24b-venice-edition:free` (si pones `OPENROUTER_API_KEY`, que es gratis).
2. **AI Horde** (sin cuenta ni tarjeta), en este orden, saltando los que no tengan workers online:
   Mistral-Nemo-2407 HERETIC, gemma-4-31B-it-heretic, Gemma-4-E4B Ultra-Uncensored-Heretic, Gemma-4-E4B HauhauCS Aggressive, L3-8B-Stheno-v3.2, gemma-3-4b-it-heretic.

Excluidos a propósito por no ser gratis permanentes: Unlid, Heabsy, Refuseless y Venice directo (necesita créditos).
`@bot modelo` te muestra cuáles están online y cuál respondió último.
Ojo: AI Horde depende de voluntarios; puede haber cola y respuestas más lentas/cortas (`HORDE_MAX_TOKENS`, 512 por defecto en tier anónimo).

## Cómo se usa
- Solo responde cuando lo **@mencionas** (o por mensaje privado).
- Charla normal → mensajes normales, sarcásticos.
- Pides código/scripts → modo serio y **cada script llega como archivo `.txt`**.
- Puede buscar en la web y en GitHub solo (el modelo decide cuándo).
- Adjunta ZIP, TAR/GZ/BZ2/XZ, 7z, RAR, APK, PDF, DOCX, XLSX, PPTX, código, logs o binarios: los lee y los analiza.
- `@bot reset` borra la memoria del canal. `@bot ayuda` lista las skills.

## Skills extra
- **GitHub completo**: descarga un repo entero (`usuario/repo` o `repo@rama`) y lo analiza.
- **DOWNLOAD**: baja un ZIP/APK/PDF por URL (máx 25MB) y lo analiza.
- **FILE**: abre un archivo concreto dentro de un ZIP/TAR/APK que subiste o descargaste (se guardan los últimos 5 por canal).
- **proyecto.zip**: cuando entrega 3 o más scripts, además te manda todo empaquetado con sus rutas reales.
- **Ejecutar código** (Python/Node/Bash, 20 s, límites de CPU/RAM): solo para los IDs en `OWNER_IDS` (Secret de Replit; tu ID de Discord con el modo desarrollador activado). Corre en el mismo servidor que el bot y no es un sandbox perfecto (tiene red y lee el disco del Repl): déjalo solo para ti.

## Hostear en Railway (24/7)
1. Sube esta carpeta a un repo de GitHub (o usa `railway up` con la CLI).
2. En railway.com: **New Project → Deploy from GitHub repo** y elige el repo. Railway detecta el `Dockerfile` y `railway.json` solo.
3. En **Variables** agrega: `DISCORD_TOKEN` (obligatoria), `OWNER_IDS` (tu ID de Discord), y opcionales `OPENROUTER_API_KEY`, `GITHUB_TOKEN`, `HORDE_API_KEY`.
4. En Discord Developer Portal activa **MESSAGE CONTENT INTENT**.
5. Deploy. El healthcheck está en `/health`; en los logs debe salir `Conectado como ...`.
Comandos nuevos: `CRYPTO`, `STACK` (Stack Overflow), `DNS`, `GHFILE`, y `WEBHOOK` ahora acepta método (`PUT url | json`) y reintenta en 429.
Seguridad: ejecutar código, webhooks y la API de GitHub siguen siendo solo para `OWNER_IDS`; si los abres a todos cualquiera podría leer tus variables (token de Discord y de GitHub) desde el servidor.

## Instalación en Replit
1. Crea un Repl de **Python** y sube todos los archivos de esta carpeta (o importa el ZIP).
2. En **Secrets** (candado) agrega:
   - `DISCORD_TOKEN`
   - (opcional) `OPENROUTER_API_KEY` y `HORDE_API_KEY`
   - (opcional) `GITHUB_TOKEN`
3. En la Shell: `pip install -r requirements.txt`
4. Pulsa **Run**.

## Discord Developer Portal
1. Crea la aplicación → Bot → copia el token.
2. En **Bot → Privileged Gateway Intents** activa **MESSAGE CONTENT INTENT** (obligatorio).
3. Invita el bot con scope `bot` y permisos: Ver canales, Enviar mensajes, Adjuntar archivos, Leer historial.

## Mantenerlo prendido 24/7
El bot levanta un mini servidor web en el puerto 8080. Opciones: Deployment (Reserved VM/Background Worker) de Replit, o hacerle ping con UptimeRobot a la URL del Repl.

## Notas
- El "entrenamiento" es el prompt de sistema en `prompt.py` (enfocado en programación, búsqueda y análisis de archivos). Edítalo cuando quieras. No es fine-tuning real.
- Los modelos gratis tienen límites y colas; el bot reintenta y cambia de modelo solo.
- Los archivos se leen en memoria (no se extraen a disco), con límites contra zip-bombs. Imágenes: solo metadatos (el modelo es de texto).
- APK: para leer el manifest completo descomenta `androguard` en `requirements.txt`.
- Variables opcionales: `MAX_TOKENS`, `TEMPERATURE`, `HISTORY_TURNS`, `FILE_CHAR_BUDGET`, `OPENROUTER_MODEL`, `HORDE_MODELS`, `PROVIDER_ORDER`.

## Build en GitHub (solo OWNER_IDS)
Con `GITHUB_TOKEN` (scopes `repo` y `workflow`): pide "apk", "exe" o "hostea la web" y el bot crea un repo, compila en GitHub Actions (APK en Ubuntu, EXE en Windows para Python/Node/Go/Rust/.NET) y te manda el archivo, o publica la web en GitHub Pages (repo público).

## Skills nuevas (v9)
TRANSLATE, CURRENCY, IP, QR, SHORT, DEFINE, STATUS, GHUSER, RELEASES, NEWS, HASH, B64, UUID, JSON.

## Modelos gratis
1. AI Horde: modelos sin censura de la comunidad (Behemoth 128B, Skyfall 31B, Dark Orthia 31B, Wicked Tutu, Rocinante, Cydonia, Stheno...). Sin cuenta.
2. Respaldo OpenRouter `:free` (necesita `OPENROUTER_API_KEY` gratis): Nemotron Ultra 550B, Gemma 4 31B, etc. Cámbialos con `OPENROUTER_FALLBACK`.
El modelo dolphin `:free` ya no existe en OpenRouter (ahora es de pago), por eso se quitó.
