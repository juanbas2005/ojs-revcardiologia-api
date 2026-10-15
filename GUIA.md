# Guía de uso — Bot OJS Cubanas + script de descarga

Esta guía explica cómo usar el bot de Telegram (`bot_telegram_simple.py` o
`bot_telegram.py` con Pyrogram) y el script `descargar_enlace.py`.

## 1. Requisitos

| Componente | Mínimo | Recomendado |
|---|---|---|
| Python | 3.8+ | 3.11 |
| Dependencias | `pip install -r requirements.txt` | lo mismo |
| Telegram bot token | de [@BotFather](https://t.me/BotFather) | — |
| Credenciales OJS | usuario `riroennis` + passwords | ver `.secrets/.env` |

Instalar dependencias:

```bash
pip install -r requirements.txt
```

## 2. Configurar credenciales

Crea el archivo `.secrets/.env` (NO se sube a git, está en `.gitignore`):

```bash
mkdir -p .secrets
cat > .secrets/.env <<'EOF'
TELEGRAM_BOT_TOKEN=123456:tu-token
OJS_USER=riroennis
OJS_PASS=Riroennis@2026          # para mil, rme, cocmed, ems, spu
OJS_PASS_REVCARD=Pupi@#1212      # para revcardiologia
# Solo si usas bot_telegram.py (Pyrogram):
# API_ID=1234567
# API_HASH=abcdef0123456789abcdef0123456789
EOF
chmod 600 .secrets/.env
```

### En GitHub Actions (recomendado para 24/7)

Ves a **Settings → Secrets and variables → Actions** del repo y añade:

| Nombre | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token de @BotFather |
| `OJS_USER` | `riroennis` |
| `OJS_PASS` | password para mil/rme/cocmed/ems/spu |
| `OJS_PASS_REVCARD` | password para revcardiologia |
| `API_ID` (opcional) | solo si usas Pyrogram |
| `API_HASH` (opcional) | solo si usas Pyrogram |

Y en **Variables** (no secrets):

| Nombre | Valor por defecto | Descripción |
|---|---|---|
| `OJS_JOURNAL` | `revcardiologia` | revista por defecto |
| `PART_MB` | `5` | tamaño de cada parte al subir (máx 19) |

## 3. Ejecutar el bot

### Opción A — bot simple (recomendada, sin api_id/hash)

```bash
python bot_telegram_simple.py
```

Usa solo `requests`. Funciona sin nada extra. **Este es el que usa el
workflow `bot_keepalive.yml` por defecto.**

### Opción B — bot con Pyrogram (más features)

Pyrogram 2.x requiere `api_id` y `api_hash` (gratuito en
<https://my.telegram.org/apps>, ~3 min):

```bash
# 1. Entra en https://my.telegram.org/apps, crea una app, copia api_id y api_hash
# 2. Añade API_ID y API_HASH a .secrets/.env o GitHub Secrets
# 3. Ejecuta:
python bot_telegram.py
```

Ventajas de Pyrogram: soporte MTProto user-mode (sesión de usuario real,
no de bot), mejores primitivas async, soporte nativo de yt-dlp integrado.

## 4. Comandos del bot

| Comando | Descripción |
|---|---|
| `/start` `/menu` `/nube` | Menú principal con botones para elegir nube OJS. |
| `/script` | Te envía `descargar_enlace.py` al chat. |
| `/config` | Muestra y cambia ajustes (revista por defecto, PART_MB). |
| `/temp` | Lista archivos temporales en `/tmp`. |
| `/limpiar` | Borra archivos temporales. |
| `/renombrar <nuevo.pdf>` | Renombra el último archivo procesado. |
| `/recortar <n_bytes>` | Deja los primeros N bytes del último archivo. |
| `/zip` | Comprime el último archivo en `.zip`. |
| `/tamaño` `/size` | Muestra el tamaño del último archivo. |
| `/ayuda` `/help` | Texto de ayuda. |

### Botones inline

- ☁️ nube → elige a qué revista OJS subir.
- 🛠 Ajustes → cambia revista por defecto y PART_MB.
- 📜 Ver script → envía `descargar_enlace.py`.
- 🧹 Limpiar temporales → borra `/tmp/tg_*`.
- 📋 Copiar enlaces → muestra los enlaces para copiar manualmente.

## 5. Flujo típico

1. Abre el bot [@Uptimeriroobot](https://t.me/Uptimeriroobot) en Telegram.
2. Toca **Start** → aparece el menú con las nubes.
3. Elige una nube (p.ej. `revcardiologia`).
4. Envía un archivo (PDF, DOCX, etc.) o un video/imagen.
5. El bot:
   - Descarga el archivo de Telegram (con barra de progreso).
   - Si la extensión no es permitida por OJS, lo renombra a `.pdf`.
   - Lo parte en trozos de `PART_MB` MB (5 por defecto).
   - Sube cada parte a OJS (con barra de progreso por parte).
   - Genera un `.txt` con los enlaces de descarga y lo envía al chat.
6. Para bajar los archivos en tu PC:
   - Descarga el `.txt` que envió el bot.
   - Ejecuta `python descargar_enlace.py enlaces.txt`.
   - Los archivos se guardan en `~/Descargas/`.

## 6. Script `descargar_enlace.py`

Descarga archivos de OJS a partir de los enlaces que genera el bot.

### Uso

```bash
# desde un archivo .txt con URLs (una por línea; # = comentario)
python descargar_enlace.py enlaces.txt

# una URL suelta
python descargar_enlace.py "https://revcardiologia.sld.cu/index.php/revcardiologia/$$$call$$$/api/file/file-api/download-file?submissionFileId=182084&submissionId=3562&stageId=1"

# URLs desde stdin
cat enlaces.txt | python descargar_enlace.py -

# cambiar directorio destino
python descargar_enlace.py enlaces.txt -o ~/Downloads
```

### Cómo funciona

1. Lee las credenciales de `.secrets/.env` (o variables de entorno).
2. Para cada URL:
   - Detecta el host y journal.
   - Hace login automático en OJS.
   - Descarga el archivo a `~/Descargas/` (con su nombre original si OJS
     lo provee).
3. Resume: `1/3 OK`, `2/3 OK`, etc.

### Importante

- Los enlaces requieren **sesión iniciada** en OJS. Si las credenciales
  fallan o el usuario no tiene permiso, verás `descarga no autorizada`.
- Para `revcardiologia` se usa `OJS_PASS_REVCARD`. Para el resto,
  `OJS_PASS`.
- El directorio destino (`~/Descargas`) se crea si no existe.

## 7. Mantener el bot 24/7

### GitHub Actions (limitado pero gratuito)

El workflow `.github/workflows/bot_keepalive.yml`:

- Se ejecuta cada 30 minutos (cron).
- Antes de arrancar, hace `getMe` para verificar el token.
- Si el token está OK, arranca el bot (con `python bot_telegram_simple.py`).
- El job termina tras 30 min sin actividad (`IDLE_MIN=30`), o al alcanzar
  el timeout de 45 min del job.
- El cron lo relanza automáticamente.

**Límites de GitHub Actions** (plan gratuito):

- 2000 minutos/mes.
- Cada job max 6 horas.
- Cron mínimo cada 5 minutos (usamos 30 min).
- El workflow `concurrency` evita solapar jobs.

### VPS / Replit / Always-on (recomendado para 24/7 real)

GitHub Actions no es ideal para bots polling. Mejor:

- **VPS** (Hetzner, DigitalOcean, etc.): `systemd` service o `pm2`.
  ```ini
  # /etc/systemd/system/ojs-bot.service
  [Unit]
  Description=Bot OJS
  After=network.target

  [Service]
  Type=simple
  User=ojs
  WorkingDirectory=/opt/ojs-bot
  EnvironmentFile=/opt/ojs-bot/.secrets/.env
  ExecStart=/usr/bin/python3 /opt/ojs-bot/bot_telegram_simple.py
  Restart=always
  RestartSec=10

  [Install]
  WantedBy=multi-user.target
  ```
  ```bash
  sudo systemctl enable --now ojs-bot
  ```

- **Replit**: usa el modo "Always-on" (de pago) o un keep-alive HTTP.

- **Resender cron**: si tu VPS se cae, instala `cron` con:
  ```cron
  */5 * * * * /opt/ojs-bot/.venv/bin/python /opt/ojs-bot/bot_telegram_simple.py
  ```

## 8. Revistas OJS soportadas

Ver [`revistas.json`](revistas.json). Resumen:

| Slug | Revista | Estado |
|---|---|---|
| `revcardiologia` | Rev. Cubana de Cardiología | ✅ login + subida + descarga OK; límite 19 MB |
| `mil` | Rev. Cubana de Medicina Militar | ✅ OK; límite 19 MB |
| `rme` | Rev. Electrónica Zoilo Marinello | ✅ OK; límite 19 MB |
| `cocmed` | Rev. Cubana de Medicina General Integral | ✅ OK; límite 19 MB |
| `ems` | Rev. Cubana de Educación Médica Superior | ✅ OK; límite 19 MB |
| `spu` | Rev. Cubana de Salud Pública | ⚠️ credenciales rechazadas; verificar |
| `medicentro` | Medicentro (Villa Clara) | ❌ OJS 2.x no compatible |

**Host de SPU**: el correcto es `revsaludpublica.sld.cu` (NO `spu.sld.cu`,
que no resuelve en DNS).

## 9. Solución de problemas

### `csrfInvalid` o `403`

- Las cookies de sesión pueden haber expirado. Reinicia el bot.
- El CSRF de OJS 3.3 caduca tras ~1h de inactividad.

### `roleBasedAccessDenied` al descargar

- El usuario `riroennis` no tiene rol adecuado en esa revista.
- Solución: pide al editor de la revista que asign el rol "Autor" o
  "Editor de sección" al usuario.

### `noFileStageId` al subir

- Significa que el archivo excede el límite PHP (`post_max_size`).
- Límite real: **19 MB** por archivo.
- Usa `PART_MB=5` o menos para partir archivos grandes.

### DNS fallido a `.sld.cu`

- Los DNS cubanos a veces fallan transitoriamente. El código reintenta 3
  veces con backoff de 8s.
- Si persiste, prueba con `curl https://revcardiologia.sld.cu` para ver si
  es tu red.

### Bot no responde en Telegram

- Verifica el token con:
  ```bash
  curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe"
  ```
- Si dice `401 Unauthorized`, el token fue revocado. Crea uno nuevo con
  [@BotFather](https://t.me/BotFather).

## 10. Pruebas realizadas (2026-10-16)

| Revista | Login | Subida 1-16 MB | Límite MB | Descarga |
|---|---|---|---|---|
| revcardiologia | ✅ | ✅ | 19 | ✅ |
| mil | ✅ | ✅ | 19 | ✅ |
| rme | ✅ | ✅ | 19 | ✅ |
| cocmed | ✅ | ✅ | 19 | ✅ |
| ems | ✅ | ✅ | 19 | ✅ |
| spu | ❌ credenciales | — | — | — |
| medicentro | — | — | OJS 2.x | — |

Submissions de prueba creadas (NO eliminadas; usuario puede borrarlas
manualmente en cada revista):
- revcardiologia: 3562, 3563
- mil: 77876, 77877
- rme: 7875
- cocmed: 5740, 5741
- ems: 5402, 5403
