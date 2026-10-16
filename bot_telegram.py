#!/usr/bin/env python3
"""
Bot de Telegram con Pyrogram — arquitectura híbrida para archivos grandes.

Estrategia:
  - Polling HTTP de getUpdates (como bot_simple, no tiene el bug de Pyrogram).
  - Handlers asíncronos con asyncio (más rápidos que el síncrono).
  - Pyrogram SOLO para descargar archivos grandes (vía MTProto, sin límite 20MB).
  - HTTP server mínimo en $PORT para Render health check.

Requiere variables de entorno:
  TELEGRAM_BOT_TOKEN  - token de @BotFather
  API_ID, API_HASH    - de https://my.telegram.org/apps
  OJS_USER, OJS_PASS, OJS_PASS_REVCARD
  PART_MB (default 5), OJS_JOURNAL (default revcardiologia),
  IDLE_MIN (default 0 = 24/7).
"""
import os
import re
import sys
import json
import time
import shutil
import zipfile
import logging
import asyncio
import threading
import traceback
from pathlib import Path
from datetime import datetime, timedelta
from collections import deque

# --- Bootstrap --------------------------------------------------------------
HERE = Path(__file__).resolve().parent
ENV_LOCAL = HERE / ".secrets" / ".env"
if not ENV_LOCAL.exists():
    ENV_LOCAL = HERE / ".env"
if ENV_LOCAL.exists():
    for line in ENV_LOCAL.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

sys.path.insert(0, str(HERE))
from ojs_api import OJSApi, cargar_revista  # noqa: E402

# --- Pyrogram (solo para descargar archivos grandes) -----------------------
try:
    from pyrogram import Client
except ImportError:
    sys.exit("Falta instalar pyrogram:  pip install pyrogram tgcrypto")

import requests
requests.packages.urllib3.disable_warnings()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("bot")

LOG_BUFFER = deque(maxlen=200)


class BufferHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append({
            "time": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "level": record.levelname,
            "msg": record.getMessage(),
        })


log.addHandler(BufferHandler())

# --- Configuración ----------------------------------------------------------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
OJS_USER = os.environ.get("OJS_USER", "")
OJS_PASS = os.environ.get("OJS_PASS", "")
OJS_PASS_REVCARD = os.environ.get("OJS_PASS_REVCARD", "")
PART_MB = int(os.environ.get("PART_MB", "5"))
JOURNAL_DEFAULT = os.environ.get("OJS_JOURNAL", "revcardiologia")
IDLE_MIN = int(os.environ.get("IDLE_MIN", "0"))

MAX_PART_MB = 19
if PART_MB > MAX_PART_MB:
    log.warning("PART_MB=%d > limite real (%d); ajustando", PART_MB, MAX_PART_MB)
    PART_MB = MAX_PART_MB

API_HTTP = f"https://api.telegram.org/bot{TOKEN}"

# revistas compatibles
with open(HERE / "revistas.json", encoding="utf-8") as f:
    REVISTAS_DICT = {r["journal"]: r for r in json.load(f)["revistas"]
                     if r.get("compatible")}
JOURNAL_SLUGS = list(REVISTAS_DICT.keys())

EXT_PERMITIDAS = {"epub", "xml", "xls", "cvs", "doc", "docx", "pdf",
                  "jpg", "png", "html", "gif", "xlsx"}

# --- Persistencia -----------------------------------------------------------
CONFIG_FILE = HERE / "config.json"


def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:
        log.warning("no pude guardar config.json: %s", e)


CONFIG = load_config()
CHOSEN = CONFIG.get("chosen", {})


def persist_chosen(chat_id, journal):
    CHOSEN[str(chat_id)] = journal
    CONFIG["chosen"] = CHOSEN
    save_config(CONFIG)


# --- Estado runtime ---------------------------------------------------------
LAST_FILE = {}
PENDING_LINKS = {}
START_TIME = time.time()
LAST_ACTIVITY = time.time()
_last_edit = {}
EDIT_THROTTLE_S = 0.8

# Cliente Pyrogram (lazy init: solo arranca cuando hace falta descargar)
_pyrogram_app = None
_pyrogram_lock = asyncio.Lock()


async def get_pyrogram():
    """Devuelve la instancia de Pyrogram (lazy init).
    Pyrogram solo se arranca cuando hace falta descargar un archivo grande."""
    global _pyrogram_app
    if _pyrogram_app is None or not _pyrogram_app.is_initialized:
        async with _pyrogram_lock:
            if _pyrogram_app is None or not _pyrogram_app.is_initialized:
                log.info(">>> Iniciando Pyrogram para descargas MTProto...")
                _pyrogram_app = Client(
                    "ojs_bot_dl",
                    api_id=API_ID, api_hash=API_HASH,
                    bot_token=TOKEN,
                    workdir=str(HERE),
                    in_memory=True,
                )
                await _pyrogram_app.start()
                me = await _pyrogram_app.get_me()
                log.info("Pyrogram listo: @%s", me.username)
    return _pyrogram_app


# --- Helpers HTTP a Telegram -----------------------------------------------


async def tg_get(method, params=None, timeout=60):
    method = method.lstrip("/")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: requests.get(f"{API_HTTP}/{method}",
                                   params=params, timeout=timeout))


async def tg_post(method, data=None, files=None, timeout=900):
    method = method.lstrip("/")
    loop = asyncio.get_event_loop()

    def _post():
        try:
            return requests.post(f"{API_HTTP}/{method}", data=data,
                                 files=files, timeout=timeout)
        except requests.Timeout:
            return requests.post(f"{API_HTTP}/{method}", data=data,
                                 files=files, timeout=900)
    return await loop.run_in_executor(None, _post)


async def tg_send(chat_id, text, reply_markup=None, parse_mode="Markdown",
                  disable_web_preview=True):
    data = {"chat_id": chat_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if disable_web_preview:
        data["disable_web_page_preview"] = "true"
    if reply_markup:
        data["reply_markup"] = json.dumps({"inline_keyboard": reply_markup})
    r = await tg_post("sendMessage", data=data, timeout=60)
    j = r.json()
    if not j.get("ok") and j.get("error_code") == 429:
        wait = j.get("parameters", {}).get("retry_after", 5)
        log.warning("FloodWait sendMessage: %ss", wait)
        await asyncio.sleep(wait + 1)
        r = await tg_post("sendMessage", data=data, timeout=60)
        j = r.json()
    return j


async def tg_edit(chat_id, msg_id, text, reply_markup=None,
                  parse_mode="Markdown", force=False):
    """Edita mensaje con throttle para evitar FloodWait."""
    key = (chat_id, msg_id)
    now = time.time()
    if not force:
        last = _last_edit.get(key, 0)
        if now - last < EDIT_THROTTLE_S:
            return None
    _last_edit[key] = now
    data = {"chat_id": chat_id, "message_id": msg_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:
        data["reply_markup"] = json.dumps({"inline_keyboard": reply_markup})
    try:
        r = await tg_post("editMessageText", data=data, timeout=30)
        return r.json()
    except Exception as e:
        log.debug("editMessageText: %s", e)
        return None


async def tg_send_document(chat_id, path, caption=""):
    def _send():
        with open(path, "rb") as f:
            r = requests.post(f"{API_HTTP}/sendDocument",
                              data={"chat_id": chat_id, "caption": caption,
                                    "parse_mode": "Markdown"},
                              files={"document": (os.path.basename(path), f)},
                              timeout=900)
        return r.json()
    loop = asyncio.get_event_loop()
    for attempt in range(3):
        try:
            return await loop.run_in_executor(None, _send)
        except Exception as e:
            log.error("sendDocument: %s", e)
            await asyncio.sleep(2)
    return None


async def answer_callback(cb_id, text=None, show_alert=False):
    if not cb_id:
        return
    data = {"callback_query_id": cb_id}
    if text:
        data["text"] = text[:200]
    if show_alert:
        data["show_alert"] = True
    await tg_post("answerCallbackQuery", data=data, timeout=10)


# --- Helpers visuales -------------------------------------------------------


def progress_bar(done, total, width=12):
    if total <= 0:
        return "░" * width + " ?"
    frac = min(done / total, 1.0)
    filled = int(frac * width)
    return "█" * filled + "░" * (width - filled)


def human_size(n):
    if n is None or n < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def human_speed(bps):
    if bps <= 0:
        return "? B/s"
    return f"{human_size(bps)}/s"


def safe_filename(name):
    base, ext = os.path.splitext(name)
    ext = ext.lstrip(".").lower()
    if ext not in EXT_PERMITIDAS:
        ext = "pdf"
    return base + "." + ext, ext


# --- Menús ------------------------------------------------------------------


def menu_keyboard():
    kb = [[{"text": f"☁️  {s}", "callback_data": f"nube:{s}"}]
          for s in JOURNAL_SLUGS]
    kb.append([
        {"text": "🛠  Ajustes", "callback_data": "config"},
        {"text": "❓  Ayuda", "callback_data": "help"},
        {"text": "📊  Estado", "callback_data": "estado"},
    ])
    kb.append([
        {"text": "📜  Script", "callback_data": "script"},
        {"text": "🧹  Limpiar", "callback_data": "limpiar"},
    ])
    return kb


async def show_menu(chat_id, edit_msg_id=None):
    actual = CHOSEN.get(str(chat_id), JOURNAL_DEFAULT)
    text = (
        f"👋 *Bot OJS Cubanas* (Pyrogram · MTProto)\n\n"
        f"☁️ *Nube actual:* `{actual}`\n"
        f"📦 *Tamaño de parte:* `{PART_MB} MB` (máx 19 MB)\n\n"
        f"*¿Qué puedo hacer?*\n"
        f"• 📤 Subir archivos de *cualquier tamaño* a OJS por partes.\n"
        f"• ⬇️ Descargar archivos grandes desde Telegram (sin límite).\n"
        f"• 🌐 Descargar enlaces directos http(s).\n"
        f"• 🎬 Renombrar, recortar, comprimir archivos.\n"
        f"• 📜 Enviarte `descargar_enlace.py`.\n\n"
        f"👇 Elige una nube o envíame un archivo/enlace."
    )
    if edit_msg_id:
        await tg_edit(chat_id, edit_msg_id, text,
                      reply_markup=menu_keyboard(), force=True)
    else:
        await tg_send(chat_id, text, reply_markup=menu_keyboard())


def help_text():
    return (
        "❓ *Ayuda del Bot OJS* (Pyrogram)\n\n"
        "*Comandos disponibles:*\n\n"
        "📤 *Subir archivos a OJS:*\n"
        "• Envía un archivo/documento/video/foto.\n"
        "• Lo divido en partes de `5 MB` (máx 19 MB).\n"
        "• Renombro a `.pdf` si la extensión no está permitida.\n"
        "• Te devuelvo un `.txt` con todos los enlaces.\n\n"
        "⬇️ *Descargar de Internet:*\n"
        "• Envía un enlace `http(s)` y lo descargo + subo.\n\n"
        "⚙️ *Comandos:*\n"
        "• `/start` `/menu` — Menú principal.\n"
        "• `/ayuda` `/help` — Esta ayuda.\n"
        "• `/estado` — Estado del bot.\n"
        "• `/config` — Ver y cambiar ajustes.\n"
        "• `/script` — Recibir `descargar_enlace.py`.\n"
        "• `/temp` — Listar archivos temporales.\n"
        "• `/limpiar` — Borrar archivos temporales.\n"
        "• `/renombrar <nombre>` — Renombrar último archivo.\n"
        "• `/recortar <bytes>` — Dejar solo los primeros N bytes.\n"
        "• `/zip` — Comprimir último archivo en `.zip`.\n"
        "• `/size` `/tamaño` — Tamaño del último archivo.\n\n"
        "✅ *Sin límite de tamaño:* descarga archivos vía MTProto.\n\n"
        "⚠️ *Límite de OJS:* máximo `19 MB` por archivo subido.\n\n"
        "💡 *Tip:* si falla una subida, usa `/limpiar` y reintenta."
    )


def estado_text():
    uptime = int(time.time() - START_TIME)
    idle = int(time.time() - LAST_ACTIVITY)
    pyr_status = "iniciado" if (_pyrogram_app and
                                 _pyrogram_app.is_initialized) else "pendiente"
    return (
        f"📊 *Estado del Bot* (Pyrogram híbrido)\n\n"
        f"• ⏱ *Uptime:* `{timedelta(seconds=uptime)}`\n"
        f"• 🕐 *Última actividad:* {idle}s atrás\n"
        f"• ☁️ *Nube por defecto:* `{JOURNAL_DEFAULT}`\n"
        f"• 📦 *PART_MB:* `{PART_MB}`\n"
        f"• 🔢 *Revistas:* `{len(JOURNAL_SLUGS)}`\n"
        f"• 🌐 *Modo:* `{'24/7 (IDLE_MIN=0)' if IDLE_MIN == 0 else f'idle {IDLE_MIN}min'}`\n"
        f"• ⚡ *Pyrogram MTProto:* `{pyr_status}`"
    )


# --- Descarga de Telegram vía MTProto (sin límite de 20 MB) ----------------


async def download_from_telegram(file_id, dest_path, nombre, chat_id,
                                  status_msg_id):
    """Descarga un archivo de Telegram vía MTProto con barra de progreso.
    Soporta archivos de CUALQUIER tamaño (sin límite de 20 MB de la HTTP API)."""
    app = await get_pyrogram()
    last_update = 0
    t0 = time.time()

    async def on_progress(current, total):
        nonlocal last_update
        now = time.time()
        if now - last_update < 0.8 and current < total:
            return
        last_update = now
        elapsed = now - t0
        speed = current / elapsed if elapsed > 0 else 0
        pct = (current / total * 100) if total else 0
        bar = progress_bar(current, total)
        eta = ((total - current) / speed) if speed > 0 else 0
        await tg_edit(chat_id, status_msg_id,
                      f"⬇️ Descargando de Telegram (MTProto)\n"
                      f"`{nombre[:40]}`\n\n"
                      f"{bar} {pct:.0f}%\n"
                      f"{human_size(current)} / {human_size(total)}\n"
                      f"⚡ {human_speed(speed)} | ETA {int(eta)}s")

    # Pyrogram 2.x: download_media acepta file_id como string
    await app.download_media(file_id, file_name=dest_path, progress=on_progress)
    return dest_path


# --- Upload a OJS por partes con barra de progreso --------------------------


async def upload_to_ojs(journal, filepath, original_name, chat_id,
                        status_msg_id):
    """Sube `filepath` a `journal` por partes. Devuelve (lines, n_parts, sub)."""
    host, _ = cargar_revista(journal)
    pw = OJS_PASS_REVCARD if journal == "revcardiologia" else OJS_PASS
    api = OJSApi(host, OJS_USER, pw, journal=journal)
    size = os.path.getsize(filepath)

    await tg_edit(chat_id, status_msg_id,
                  f"🔐 Logueando en *{journal}*...", force=True)
    try:
        api.login()
    except Exception as e:
        raise RuntimeError(f"login {journal}: {e}")

    await tg_edit(chat_id, status_msg_id,
                  f"📝 Creando submission en *{journal}*...", force=True)
    try:
        sub = int(api.create())
    except Exception as e:
        raise RuntimeError(f"crear submission {journal}: {e}")

    base, ext = safe_filename(original_name)
    total_parts = max(1, (size + PART_MB * 1024 * 1024 - 1) //
                      (PART_MB * 1024 * 1024))

    lines = [
        f"# «{original_name}» ({size} bytes), {PART_MB} MB por parte",
        f"# Submission {sub} en {journal}",
        f"# Fecha: {datetime.now().isoformat(timespec='seconds')}",
    ]
    idx = 0
    t_start = time.time()
    loop = asyncio.get_event_loop()
    with open(filepath, "rb") as f:
        while True:
            data = f.read(PART_MB * 1024 * 1024)
            if not data:
                break
            idx += 1
            part_path = f"/tmp/{base}_parte{idx:03d}.{ext}"
            with open(part_path, "wb") as g:
                g.write(data)
            part_size = len(data)
            part_t0 = time.time()

            def cb(s, total, _idx=idx, _tot=total_parts,
                   _t0=part_t0, _chat=chat_id, _msg=status_msg_id,
                   _loop=loop, _journal=journal):
                now = time.time()
                elapsed = now - _t0
                speed = s / elapsed if elapsed > 0 else 0
                pct = (s / total * 100) if total else 0
                bar = progress_bar(s, total)
                eta = ((total - s) / speed) if speed > 0 else 0
                global_pct = ((_idx - 1) + pct / 100) / _tot * 100
                text = (
                    f"☁️ Subiendo a *{_journal}* (parte {_idx}/{_tot})\n\n"
                    f"{bar} {pct:.0f}% de esta parte\n"
                    f"{human_size(s)} / {human_size(total)}\n"
                    f"⚡ {human_speed(speed)} | ETA {int(eta)}s\n\n"
                    f"📊 Global: {global_pct:.0f}% "
                    f"({_idx-1}/{_tot} partes)"
                )
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        tg_edit(_chat, _msg, text), _loop)
                    fut.result(timeout=2)
                except Exception:
                    pass

            try:
                res = await loop.run_in_executor(
                    None,
                    lambda p=part_path: api.upload(
                        sub, p,
                        filename=os.path.basename(part_path),
                        progress_cb=cb))
                lines.append(api.download_url(sub, res["id"]))
            except Exception as e:
                err = str(e)[:200]
                lines.append(f"# ERROR en parte {idx}: {err}")
                await tg_edit(chat_id, status_msg_id,
                              f"⚠️ Error en parte {idx}/{total_parts}: "
                              f"`{err}`\nContinuando...", force=True)
            try:
                os.remove(part_path)
            except OSError:
                pass
    elapsed = time.time() - t_start
    await tg_edit(chat_id, status_msg_id,
                  f"✅ Subida completada en *{journal}*\n"
                  f"`{idx}` partes en `{int(elapsed)}s`", force=True)
    return lines, idx, sub


# --- Descarga de URL directa -----------------------------------------------


async def download_url_direct(url, dest_dir, chat_id, status_msg_id):
    fname = url.rstrip("/").split("/")[-1] or "archivo.bin"
    fname = fname.split("?")[0]
    fname = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", fname)[:100]
    if not fname:
        fname = "archivo.bin"
    dest = os.path.join(dest_dir, fname)

    def _download():
        r = requests.get(url, stream=True, timeout=60, verify=False,
                         headers={"User-Agent": "Mozilla/5.0 (X11; Linux)"})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        total = int(r.headers.get("Content-Length", 0))
        written = 0
        last_edit = 0
        t0 = time.time()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(128 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                written += len(chunk)
        return dest, total, t0

    loop = asyncio.get_event_loop()
    dest, total, t0 = await loop.run_in_executor(None, _download)
    # Como no podemos reportar progreso desde el thread, lo reportamos al final
    await tg_edit(chat_id, status_msg_id,
                  f"✅ Descargado: `{os.path.basename(dest)}` "
                  f"({human_size(os.path.getsize(dest))})")
    return dest


# --- Procesar archivo recibido ----------------------------------------------


async def process_upload(msg_data):
    """Procesa un archivo recibido: lo descarga y sube a OJS."""
    chat_id = msg_data["chat"]["id"]

    # Detectar archivo/media
    doc = msg_data.get("document")
    vid = msg_data.get("video")
    aud = msg_data.get("audio")
    anim = msg_data.get("animation")
    voice = msg_data.get("voice")
    vnote = msg_data.get("video_note")
    photo = msg_data.get("photo")

    media = None
    file_id = None
    nombre = None
    size = 0

    if doc:
        media = doc
        file_id = doc.get("file_id")
        nombre = doc.get("file_name") or f"doc_{int(time.time())}.bin"
        size = doc.get("file_size", 0)
    elif vid:
        media = vid
        file_id = vid.get("file_id")
        nombre = vid.get("file_name") or f"video_{int(time.time())}.mp4"
        size = vid.get("file_size", 0)
    elif aud:
        media = aud
        file_id = aud.get("file_id")
        nombre = aud.get("file_name") or f"audio_{int(time.time())}.mp3"
        size = aud.get("file_size", 0)
    elif anim:
        media = anim
        file_id = anim.get("file_id")
        nombre = f"gif_{int(time.time())}.mp4"
        size = anim.get("file_size", 0)
    elif voice:
        media = voice
        file_id = voice.get("file_id")
        nombre = f"voice_{int(time.time())}.ogg"
        size = voice.get("file_size", 0)
    elif vnote:
        media = vnote
        file_id = vnote.get("file_id")
        nombre = f"videonote_{int(time.time())}.mp4"
        size = vnote.get("file_size", 0)
    elif photo:
        # última = mayor resolución
        biggest = photo[-1]
        media = biggest
        file_id = biggest.get("file_id")
        nombre = f"foto_{int(time.time())}.jpg"
        size = biggest.get("file_size", 0)

    if not file_id:
        await tg_send(chat_id, "❌ No detecté ningún archivo en el mensaje.")
        return

    journal = CHOSEN.get(str(chat_id), JOURNAL_DEFAULT)
    if journal not in REVISTAS_DICT:
        await tg_send(chat_id, f"❌ Revista no válida: `{journal}`. "
                               f"Usa /start para elegir.")
        return

    size_human = human_size(size)
    status = await tg_send(chat_id,
                           f"📥 Recibí `{nombre[:50]}` ({size_human}). "
                           f"Nube: *{journal}*")
    status_msg_id = (status or {}).get("result", {}).get("message_id")
    if not status_msg_id:
        # reintentar sin markdown
        status = await tg_send(chat_id,
                               f"Recibi {nombre[:50]} ({size_human}). "
                               f"Nube: {journal}", parse_mode=None)
        status_msg_id = (status or {}).get("result", {}).get("message_id")

    safe_name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", nombre)[:100]
    local = f"/tmp/tg_{int(time.time())}_{safe_name}"

    # 1. Descargar de Telegram (Pyrogram MTProto)
    try:
        await download_from_telegram(file_id, local, nombre, chat_id,
                                     status_msg_id)
    except Exception as e:
        await tg_send(chat_id, f"❌ Descarga falló: `{str(e)[:200]}`")
        try:
            os.remove(local)
        except OSError:
            pass
        return

    actual_size = os.path.getsize(local)
    LAST_FILE[chat_id] = local

    # 2. Subir a OJS
    try:
        lines, n_parts, sub = await upload_to_ojs(
            journal, local, nombre, chat_id, status_msg_id)
    except Exception as e:
        await tg_send(chat_id, f"❌ Error subiendo a *{journal}*:\n"
                               f"`{str(e)[:300]}`")
        return

    # 3. Generar .txt con enlaces
    txt = f"/tmp/enlaces_{Path(safe_name).stem}.txt"
    with open(txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    enlaces = [l for l in lines if not l.startswith("#")]
    errores = [l for l in lines if l.startswith("# ERROR")]
    if enlaces:
        PENDING_LINKS[str(chat_id)] = enlaces

    # 4. Botones
    kb = [
        [{"text": "📋  Ver enlaces", "callback_data": "ver_enlaces"},
         {"text": "📜  Script", "callback_data": "script"}],
        [{"text": "☁️  Menú", "callback_data": "menu"},
         {"text": "🛠  Ajustes", "callback_data": "config"}],
    ]

    caption = (f"✅ *{n_parts} partes* subidas a *{journal}*\n"
               f"📦 Submission: `{sub}`\n"
               f"📊 Tamaño: `{human_size(actual_size)}`")
    if errores:
        caption += f"\n⚠️ {len(errores)} parte(s) con error (ver .txt)"

    await tg_send_document(chat_id, txt, caption=caption)

    if len(enlaces) <= 4:
        enlace_text = "\n".join(f"`{e}`" for e in enlaces)
        await tg_send(chat_id, f"📋 *Enlaces generados:*\n\n{enlace_text}",
                      reply_markup=kb)
    else:
        await tg_send(chat_id, f"📋 *{len(enlaces)} enlaces* en el .txt adjunto "
                               f"arriba. Toca *Ver enlaces* para mostrarlos.",
                      reply_markup=kb)


# --- Dispatcher (handlers de texto y callbacks) ----------------------------


async def handle_text(chat_id, text):
    global LAST_ACTIVITY, PART_MB
    LAST_ACTIVITY = time.time()
    text = (text or "").strip()

    if text.startswith("/start") or text in ("/menu", "/nube"):
        await show_menu(chat_id)
        return
    if text.startswith("/ayuda") or text == "/help":
        await tg_send(chat_id, help_text())
        return
    if text.startswith("/estado"):
        await tg_send(chat_id, estado_text())
        return
    if text.startswith("/script"):
        script_path = HERE / "descargar_enlace.py"
        if script_path.exists():
            await tg_send_document(chat_id, str(script_path),
                                   caption="📜 *descargar_enlace.py*")
        else:
            await tg_send(chat_id, "❌ No encuentro `descargar_enlace.py`.")
        return
    if text.startswith("/temp"):
        files = sorted(Path("/tmp").glob("tg_*"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:20]
        if not files:
            await tg_send(chat_id, "✅ No hay archivos temporales en `/tmp`.")
            return
        msg = "🧹 *Temporales* (últimos 20):\n\n"
        total = 0
        for p in files:
            sz = p.stat().st_size
            total += sz
            msg += f"• `{p.name}` ({human_size(sz)})\n"
        msg += f"\n📊 *Total:* {human_size(total)}\n\nUsa `/limpiar` para borrar."
        await tg_send(chat_id, msg)
        return
    if text.startswith("/limpiar"):
        n, freed = 0, 0
        for p in Path("/tmp").glob("tg_*"):
            try:
                sz = p.stat().st_size
                p.unlink()
                n += 1
                freed += sz
            except OSError:
                pass
        await tg_send(chat_id, f"🧹 Limpié *{n}* archivos "
                               f"({human_size(freed)} liberados).")
        return
    if text.startswith("/config"):
        kb = [[{"text": f"📡  {s}", "callback_data": f"setdef:{s}"}]
              for s in JOURNAL_SLUGS]
        kb.append([
            {"text": "➕ PART_MB +1", "callback_data": "partmb:1"},
            {"text": "➖ PART_MB -1", "callback_data": "partmb:-1"},
            {"text": "↩️ 5 MB", "callback_data": "partmb:5"},
        ])
        actual = CHOSEN.get(str(chat_id), JOURNAL_DEFAULT)
        await tg_send(chat_id,
                      f"⚙️ *Ajustes*\n\n"
                      f"• Revista por defecto: `{actual}`\n"
                      f"• PART_MB: `{PART_MB}` MB (límite OJS: 19 MB)\n"
                      f"• Idle: `{IDLE_MIN}` min\n"
                      f"• Bot: `@Uptimeriroobot` (Pyrogram híbrido)\n\n"
                      f"👇 Toca para cambiar:",
                      reply_markup=kb)
        return
    if text.startswith("/renombrar"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await tg_send(chat_id, "Uso: `/renombrar nuevo_nombre.pdf`")
            return
        new_name = parts[1].strip()
        p = LAST_FILE.get(chat_id)
        if not p or not os.path.exists(p):
            await tg_send(chat_id, "❌ No hay archivo reciente. Envíame uno primero.")
            return
        new_path = os.path.join(os.path.dirname(p), new_name)
        try:
            os.rename(p, new_path)
            LAST_FILE[chat_id] = new_path
            await tg_send_document(chat_id, new_path,
                                   caption=f"✅ Renombrado a `{new_name}`")
        except OSError as e:
            await tg_send(chat_id, f"❌ No pude renombrar: `{e}`")
        return
    if text.startswith("/recortar"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            await tg_send(chat_id, "Uso: `/recortar 1000000` (deja los primeros N bytes)")
            return
        n = int(parts[1].strip())
        p = LAST_FILE.get(chat_id)
        if not p or not os.path.exists(p):
            await tg_send(chat_id, "❌ No hay archivo reciente. Envíame uno primero.")
            return
        new_path = p + ".recortado"
        try:
            with open(p, "rb") as f, open(new_path, "wb") as g:
                g.write(f.read(n))
            LAST_FILE[chat_id] = new_path
            await tg_send_document(chat_id, new_path,
                                   caption=f"✅ Recortado a `{human_size(n)}`")
        except OSError as e:
            await tg_send(chat_id, f"❌ No pude recortar: `{e}`")
        return
    if text.startswith("/zip"):
        p = LAST_FILE.get(chat_id)
        if not p or not os.path.exists(p):
            await tg_send(chat_id, "❌ No hay archivo reciente. Envíame uno primero.")
            return
        zip_path = p + ".zip"
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(p, os.path.basename(p))
            LAST_FILE[chat_id] = zip_path
            await tg_send_document(chat_id, zip_path,
                                   caption=f"✅ Comprimido en `.zip` "
                                           f"({human_size(os.path.getsize(zip_path))})")
        except Exception as e:
            await tg_send(chat_id, f"❌ No pude comprimir: `{e}`")
        return
    if text.startswith("/tamano") or text.startswith("/tamaño") or \
            text.startswith("/size"):
        p = LAST_FILE.get(chat_id)
        if not p or not os.path.exists(p):
            await tg_send(chat_id, "❌ No hay archivo reciente. Envíame uno primero.")
            return
        await tg_send(chat_id, f"📄 `{os.path.basename(p)}`\n"
                               f"📊 Tamaño: `{human_size(os.path.getsize(p))}`")
        return

    # URL directa?
    if text.startswith("http://") or text.startswith("https://"):
        status = await tg_send(chat_id, "🔗 Procesando enlace...")
        status_msg_id = (status or {}).get("result", {}).get("message_id")
        if not status_msg_id:
            return
        try:
            local = await download_url_direct(text, "/tmp", chat_id,
                                                status_msg_id)
        except Exception as e:
            await tg_send(chat_id, f"❌ Descarga falló: `{str(e)[:200]}`")
            return
        if not local or not os.path.exists(local):
            await tg_send(chat_id, "❌ Descarga falló: archivo no encontrado.")
            return
        LAST_FILE[chat_id] = local
        size = os.path.getsize(local)
        await tg_send(chat_id, f"✅ Descargado: `{os.path.basename(local)}` "
                               f"({human_size(size)}). Subiendo a OJS...")
        journal = CHOSEN.get(str(chat_id), JOURNAL_DEFAULT)
        try:
            lines, n_parts, sub = await upload_to_ojs(
                journal, local, os.path.basename(local), chat_id, status_msg_id)
        except Exception as e:
            await tg_send(chat_id, f"❌ Error subiendo: `{str(e)[:200]}`")
            return
        txt = f"/tmp/enlaces_{Path(local).stem}.txt"
        with open(txt, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        enlaces = [l for l in lines if not l.startswith("#")]
        if enlaces:
            PENDING_LINKS[str(chat_id)] = enlaces
        await tg_send_document(chat_id, txt,
                               caption=f"✅ {n_parts} partes en *{journal}* "
                                       f"(sub `{sub}`)")
        return

    # Texto no reconocido
    await tg_send(chat_id,
                  "🤔 No reconocí ese mensaje.\n\n"
                  "Puedes:\n"
                  "• Enviarme un *archivo* (PDF, video, etc.).\n"
                  "• Enviarme un *enlace* http(s).\n"
                  "• Usar /start para el menú.\n"
                  "• Usar /ayuda para ver todos los comandos.")


async def handle_callback(cb):
    global PART_MB, LAST_ACTIVITY
    LAST_ACTIVITY = time.time()
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    data = cb.get("data", "")
    cb_id = cb.get("id")

    try:
        await answer_callback(cb_id)
    except Exception:
        pass

    if data == "help":
        await tg_send(chat_id, help_text())
        return
    if data == "estado":
        await tg_send(chat_id, estado_text())
        return
    if data == "menu":
        await show_menu(chat_id, edit_msg_id=msg_id)
        return
    if data == "config":
        kb = [[{"text": f"📡  {s}", "callback_data": f"setdef:{s}"}]
              for s in JOURNAL_SLUGS]
        kb.append([
            {"text": "➕ PART_MB +1", "callback_data": "partmb:1"},
            {"text": "➖ PART_MB -1", "callback_data": "partmb:-1"},
            {"text": "↩️ 5 MB", "callback_data": "partmb:5"},
        ])
        actual = CHOSEN.get(str(chat_id), JOURNAL_DEFAULT)
        await tg_edit(chat_id, msg_id,
                     f"⚙️ *Ajustes*\n\n"
                     f"• Revista: `{actual}`\n"
                     f"• PART_MB: `{PART_MB}` MB\n"
                     f"• Idle: `{IDLE_MIN}` min\n\n"
                     f"👇 Toca para cambiar:",
                     reply_markup=kb, force=True)
        return
    if data == "script":
        script_path = HERE / "descargar_enlace.py"
        if script_path.exists():
            await tg_send_document(chat_id, str(script_path),
                                   caption="📜 *descargar_enlace.py*")
        else:
            await tg_send(chat_id, "❌ No encuentro `descargar_enlace.py`.")
        return
    if data == "limpiar":
        n, freed = 0, 0
        for p in Path("/tmp").glob("tg_*"):
            try:
                sz = p.stat().st_size
                p.unlink()
                n += 1
                freed += sz
            except OSError:
                pass
        await tg_edit(chat_id, msg_id, f"🧹 Limpié *{n}* archivos "
                                      f"({human_size(freed)} liberados).",
                      force=True)
        return
    if data == "ver_enlaces":
        enlaces = PENDING_LINKS.get(str(chat_id), [])
        if not enlaces:
            await tg_send(chat_id, "❌ No hay enlaces recientes. "
                                   "Sube un archivo primero.")
            return
        if len(enlaces) <= 4:
            text = "\n".join(f"`{e}`" for e in enlaces)
        else:
            text = "\n".join(f"`{e}`" for e in enlaces[:8])
            text += f"\n\n... y {len(enlaces)-8} más."
        kb = [[{"text": "📋  Copiar", "callback_data": "copiar_enlaces"}]]
        await tg_send(chat_id, f"📋 *Enlaces:*\n\n{text}", reply_markup=kb)
        return
    if data == "copiar_enlaces":
        enlaces = PENDING_LINKS.get(str(chat_id), [])
        if enlaces:
            text = "\n".join(enlaces)
            for i in range(0, len(text), 4000):
                await tg_send(chat_id, f"```\n{text[i:i+4000]}\n```")
        return

    if data.startswith("nube:"):
        slug = data.split(":", 1)[1]
        if slug not in REVISTAS_DICT:
            await answer_callback(cb_id, "Revista no válida", show_alert=True)
            return
        persist_chosen(chat_id, slug)
        await answer_callback(cb_id, f"✅ Nube: {slug}")
        await tg_edit(chat_id, msg_id,
                      f"✅ *Nube elegida:* `{slug}`\n\n"
                      f"Ahora envíame el archivo que quieres subir.",
                      force=True)
        return

    if data.startswith("setdef:"):
        slug = data.split(":", 1)[1]
        if slug not in REVISTAS_DICT:
            await answer_callback(cb_id, "Revista no válida", show_alert=True)
            return
        persist_chosen(chat_id, slug)
        await answer_callback(cb_id, f"✅ Por defecto: {slug}")
        await tg_edit(chat_id, msg_id,
                      f"✅ *Revista por defecto:* `{slug}`", force=True)
        return

    if data.startswith("partmb:"):
        op = data.split(":", 1)[1]
        old = PART_MB
        if op == "5":
            PART_MB = 5
        else:
            try:
                PART_MB = max(1, min(MAX_PART_MB, PART_MB + int(op)))
            except ValueError:
                pass
        await answer_callback(cb_id, f"✅ PART_MB = {PART_MB} MB")
        await tg_edit(chat_id, msg_id,
                      f"✅ *PART_MB = `{PART_MB}` MB*\n"
                      f"(antes: {old} MB, límite OJS: {MAX_PART_MB} MB)",
                      force=True)
        return

    await answer_callback(cb_id, "Acción no reconocida", show_alert=True)


async def dispatch_update(up):
    """Procesa un update (message o callback_query)."""
    try:
        if "callback_query" in up:
            await handle_callback(up["callback_query"])
        elif up.get("message"):
            msg = up["message"]
            text = (msg.get("text") or "").strip()
            if text:
                await handle_text(msg["chat"]["id"], text)
            elif (msg.get("document") or msg.get("video") or
                  msg.get("audio") or msg.get("animation") or
                  msg.get("voice") or msg.get("video_note") or
                  msg.get("photo")):
                await process_upload(msg)
        elif up.get("edited_message"):
            # ignorar ediciones
            pass
    except Exception as e:
        log.exception("dispatch error: %s", e)


# --- Polling HTTP loop -----------------------------------------------------


async def polling_loop():
    """Long polling HTTP a getUpdates. Procesa updates con dispatch_update."""
    global LAST_ACTIVITY
    offset = 0
    log.info(">>> Iniciando polling HTTP de getUpdates...")
    while True:
        try:
            r = await tg_get("/getUpdates",
                             params={"timeout": 25, "offset": offset,
                                     "allowed_updates": json.dumps(
                                         ["message", "edited_message",
                                          "callback_query"])},
                             timeout=60)
            data = r.json()
            if not data.get("ok"):
                log.warning("getUpdates error: %s", data)
                if data.get("error_code") == 409:
                    # otro bot está corriendo; esperar más
                    await asyncio.sleep(30)
                else:
                    await asyncio.sleep(5)
                continue
            for up in data.get("result", []):
                offset = up["update_id"] + 1
                LAST_ACTIVITY = time.time()
                # dispatch en background (no bloquear el polling)
                asyncio.create_task(dispatch_update(up))
        except Exception as e:
            log.warning("polling loop error: %s", e)
            await asyncio.sleep(5)


# --- Idle watcher ----------------------------------------------------------


async def idle_watcher():
    while True:
        await asyncio.sleep(60)
        if IDLE_MIN > 0 and time.time() - LAST_ACTIVITY > IDLE_MIN * 60:
            log.info("Sin actividad por %d min, cerrando.", IDLE_MIN)
            return


# --- HTTP server para Render health check ----------------------------------


def _run_http_server(port):
    import socketserver
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path in ("/", "/health", "/ping"):
                body = (f"OK\nbot_alive=running\n"
                        f"pending_links_chats={len(PENDING_LINKS)}\n"
                        f"logs={len(LOG_BUFFER)}\n"
                        f"pyrogram={'on' if (_pyrogram_app and _pyrogram_app.is_initialized) else 'pending'}\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/logs":
                body = json.dumps(list(LOG_BUFFER), ensure_ascii=False,
                                  indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/status":
                body = json.dumps({
                    "bot_alive": True,
                    "uptime_s": int(time.time() - START_TIME),
                    "last_activity_s": int(time.time() - LAST_ACTIVITY),
                    "pending_links_chats": len(PENDING_LINKS),
                    "chosen": CHOSEN,
                    "pyrogram_started": bool(
                        _pyrogram_app and _pyrogram_app.is_initialized),
                    "log_buffer_size": len(LOG_BUFFER),
                    "recent_logs": list(LOG_BUFFER)[-10:],
                }, ensure_ascii=False, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    try:
        socketserver.TCPServer.allow_reuse_address = True
        server = socketserver.TCPServer(("0.0.0.0", port), Handler)
        log.info("HTTP health server en puerto %d", port)
        server.serve_forever()
    except Exception as e:
        log.warning("HTTP server no arrancó: %s", e)


def start_http_server_if_needed():
    port = int(os.environ.get("PORT", "0"))
    if not port:
        return
    t = threading.Thread(target=_run_http_server, args=(port,),
                         daemon=True, name="http")
    t.start()


# --- Main ------------------------------------------------------------------


async def main_async():
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.time()

    start_http_server_if_needed()

    log.info(">>> Bot iniciando (arquitectura híbrida HTTP + Pyrogram lazy)...")
    log.info("Config: API_ID=%s, PART_MB=%d, JOURNAL_DEFAULT=%s, IDLE_MIN=%d",
             API_ID, PART_MB, JOURNAL_DEFAULT, IDLE_MIN)
    log.info("Polling HTTP de getUpdates: ACTIVO")
    log.info("Pyrogram MTProto: se iniciará cuando llegue el primer archivo")

    asyncio.create_task(idle_watcher())
    asyncio.create_task(polling_loop())

    while True:
        await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("Interrumpido por usuario.")
