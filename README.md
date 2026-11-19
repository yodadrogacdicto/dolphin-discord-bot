# Dolphin — Bot de Discord con modelos gratuitos

Usa AI Horde sin pago como proveedor principal y, si configuras una clave gratuita, puede usar como respaldo solo modelos de OpenRouter cuyo ID termina en `:free` y cuyo nombre coincide con el catálogo de modelos uncensored/abliterated. Consulta el catálogo de Horde en vivo para elegir modelos con workers disponibles; el bot no puede garantizar que estén online, sean rápidos o nunca rechacen una solicitud. `@bot modelo` muestra la disponibilidad consultada.

## Cómo se usa
- Solo responde cuando lo **@mencionas** (o por mensaje privado).
- Charla normal → mensajes normales, sarcásticos.
- Pides código/scripts → modo serio y cada archivo conserva su extensión real (por ejemplo `index.html`).
- Puede buscar en la web cuando se lo pides y abre enlaces HTTP(S) públicos que incluyas.
- Adjunta ZIP, TAR/GZ/BZ2/XZ, 7z, RAR, APK, PDF, DOCX, XLSX, PPTX, código, logs o binarios: los lee y los analiza.
- `@bot reset` borra el historial temporal del canal. `@bot ayuda` lista las skills.
- En DM, `@bot recuerda: ...` guarda un dato; `@bot memoria` lo lista; `@bot olvida 3` borra el recuerdo 3; `@bot olvida mi memoria` los borra todos. La memoria es explícita, privada por usuario y no se incluye en mensajes de servidores.

## Skills extra
- **GitHub completo**: descarga un repo entero (`usuario/repo` o `repo@rama`) y lo analiza.
- **DOWNLOAD**: baja un ZIP/APK/PDF por URL (máx 25MB) y lo analiza.
- **FILE**: abre un archivo concreto dentro de un ZIP/TAR/APK que subiste o descargaste (se guardan los últimos 5 por canal).
- **proyecto.zip**: cuando entrega 3 o más scripts, además te manda todo empaquetado con sus rutas reales.
- **Ejecutar código** (Python/Node/Bash, 20 s, límites de CPU/RAM): solo para los IDs en `OWNER_IDS` (Secret de Replit; tu ID de Discord con el modo desarrollador activado). Corre en el mismo servidor que el bot y no es un sandbox perfecto (tiene red y lee el disco del Repl): déjalo solo para ti.

## Publicar una web y conectar un formulario a Discord

Al pedir que suba una página web a GitHub, Dolphin usa GitHub Pages. Para que un formulario envíe datos a Discord, el HTML generado llama al relay del servidor en `POST /api/webhook`; **no pongas el token del webhook en `index.html`**.

En las variables de Railway configura:
- `DISCORD_WEBHOOK_URL`: URL secreta del webhook de Discord.
- `WEBHOOK_ALLOWED_ORIGINS`: origen exacto del sitio, por ejemplo `https://tuusuario.github.io`.
- `WEBHOOK_API_URL`: URL pública del servicio Railway, por ejemplo `https://tu-bot.up.railway.app`.

El relay valida el origen, limita el tamaño y ritmo de envíos, evita menciones de Discord y no expone la URL secreta. El origen permitido no sustituye CAPTCHA/autenticación: cualquier servicio público puede recibir abuso; limita el formulario a los datos que necesites.

## Hostear en Railway (24/7)
1. Sube esta carpeta a un repo de GitHub (o usa `railway up` con la CLI).
2. En railway.com: **New Project → Deploy from GitHub repo** y elige el repo. Railway detecta el `Dockerfile` y `railway.json` solo.
3. En **Variables** agrega `DISCORD_TOKEN`, tu `OWNER_IDS`, y opcionalmente `GITHUB_TOKEN`, `HORDE_API_KEY`, `OPENROUTER_API_KEY` y las tres variables del relay.
4. En Discord Developer Portal activa **MESSAGE CONTENT INTENT**.
5. Para conservar recuerdos entre redeploys, adjunta un volumen Railway en `/app/data` (o configura `MEMORY_DB_PATH` dentro del volumen). Sin volumen persistente, SQLite puede perderse al recrear el contenedor.
6. Deploy. El healthcheck está en `/health`; en los logs debe salir `Conectado como ...`.
La herramienta directa `WEBHOOK` y la API de GitHub siguen limitadas a `OWNER_IDS`.

## Instalación en Replit
1. Crea un Repl de **Python** y sube todos los archivos de esta carpeta (o importa el ZIP).
2. En **Secrets** agrega `DISCORD_TOKEN`; opcionalmente `HORDE_API_KEY`, `OPENROUTER_API_KEY`, `GITHUB_TOKEN` y las variables de relay/memoria.
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
- Variables opcionales: `MAX_TOKENS`, `TEMPERATURE`, `HISTORY_TURNS`, `FILE_CHAR_BUDGET`, `HORDE_MODELS`, `VENICE_MODELS`, `UNLID_MODELS`, `PROVIDER_ORDER`.

## Build en GitHub (solo OWNER_IDS)
Con `GITHUB_TOKEN`, pide "sube la página web a GitHub" para publicarla en GitHub Pages; para APK/EXE, el token también necesita permiso de Actions (`workflow`). El bot informa el resultado de la publicación o el error de GitHub.

## Skills nuevas (v9)
TRANSLATE, CURRENCY, IP, QR, SHORT, DEFINE, STATUS, GHUSER, RELEASES, NEWS, HASH, B64, UUID, JSON.

## Modelos gratuitos incluidos
- **AI Horde:** búsqueda del catálogo en vivo; hoy se detectaron modelos como Qwen3.8 Flash Next Uncensored, Cydonia 24B Heretic y MeroMero 26B Uncensored Heretic, además de Behemoth, Skyfall, Stheno, Rocinante y otros. La cantidad de workers cambia continuamente.
- **OpenRouter (opcional):** solo modelos con sufijo `:free`; el respaldo conocido es `cognitivecomputations/dolphin-mistral-24b-venice-edition:free`. Requiere una clave de cuenta y puede tener límites/cuotas variables.
- “Uncensored” describe el ajuste/modelo, no garantiza respuestas sin negativas. El bot no cambia de modelo para tratar de eludir una negativa.
