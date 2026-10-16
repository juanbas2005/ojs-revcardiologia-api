#!/usr/bin/env python3
"""
Bot de Telegram con Pyrogram — para descarga de archivos de cualquier tamaño.

Reescritura limpia y robusta con Pyrogram 2.x + MTProto. Soporta archivos
de CUALQUIER tamaño (sin el límite de 20 MB de la API de bots HTTP).

Requiere variables de entorno:
  TELEGRAM_BOT_TOKEN  - token de @BotFather
  API_ID              - de https://my.telegram.org/apps
  API_HASH            - de https://my.telegram.org/apps
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

# --- Pyrogram (debe estar instalado: pip install pyrogram tgcrypto) --------
try:
    from pyrogram import Client, filters
    from pyrogram.types import (
        Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup)
    from pyrogram.errors import (
        MessageNotModified, FloodWait, MessageTooLong, RPCError)
except ImportError:
    sys.exit("Falta instalar pyrogram:  pip install pyrogram tgcrypto")

import requests  # para descarga de URLs directas
requests.packages.urllib3.disable_warnings()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("bot")

# Buffer de logs en memoria (para diagnóstico via HTTP /logs)
LOG_BUFFER = deque(maxlen=100)


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
IDLE_MIN = int(os.environ.get("IDLE_MIN", "0"))  # 0 = 24/7

MAX_PART_MB = 19
if PART_MB > MAX_PART_MB:
    log.warning("PART_MB=%d > limite real (%d); ajustando", PART_MB, MAX_PART_MB)
    PART_MB = MAX_PART_MB

# Revistas compatibles (leídas de revistas.json)
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
LAST_FILE = {}            # chat_id -> path local del último archivo
PENDING_LINKS = {}        # chat_id -> lista de enlaces pendientes
START_TIME = time.time()
LAST_ACTIVITY = time.time()

# Throttle de ediciones: máximo 1 edición cada 0.8s por mensaje
_last_edit = {}
EDIT_THROTTLE_S = 0.8

# Cliente Pyrogram (en memoria, sin archivos .session)
app = Client(
    "ojs_bot",
    api_id=API_ID, api_hash=API_HASH,
    bot_token=TOKEN,
    workdir=str(HERE),
    in_memory=True,
)


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


# --- Helpers de Telegram ----------------------------------------------------


async def tg_send(chat_id, text, reply_markup=None, parse_mode="Markdown",
                  disable_web_preview=True):
    """Envía mensaje con manejo de FloodWait y troceado."""
    for attempt in range(3):
        try:
            return await app.send_message(
                chat_id, text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_preview)
        except FloodWait as e:
            log.warning("FloodWait sendMessage: %ss", e.value)
            await asyncio.sleep(e.value + 1)
        except MessageTooLong:
            # Trocear
            for i in range(0, len(text), 4000):
                try:
                    await app.send_message(chat_id, text[i:i+4000],
                                           parse_mode=parse_mode)
                except Exception:
                    pass
            return None
        except Exception as e:
            log.error("sendMessage: %s", e)
            return None
    return None


async def tg_edit(msg, text, reply_markup=None, parse_mode="Markdown",
                  force=False):
    """Edita mensaje con throttle para evitar FloodWait."""
    if msg is None:
        return None
    key = (msg.chat.id, msg.id)
    now = time.time()
    if not force:
        last = _last_edit.get(key, 0)
        if now - last < EDIT_THROTTLE_S:
            return None
    _last_edit[key] = now
    try:
        return await msg.edit_text(text, reply_markup=reply_markup,
                                   parse_mode=parse_mode)
    except MessageNotModified:
        return None
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        try:
            return await msg.edit_text(text, reply_markup=reply_markup,
                                       parse_mode=parse_mode)
        except Exception:
            return None
    except Exception as e:
        log.debug("edit_text: %s", e)
        return None


async def tg_send_document(chat_id, path, caption=""):
    """Envía documento con retry."""
    for attempt in range(3):
        try:
            return await app.send_document(
                chat_id, path, caption=caption, parse_mode="Markdown")
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            log.error("sendDocument: %s", e)
            return None
    return None


# --- Menús ------------------------------------------------------------------


def menu_keyboard():
    kb = [[InlineKeyboardButton(f"☁️  {s}", callback_data=f"nube:{s}")]
          for s in JOURNAL_SLUGS]
    kb.append([
        InlineKeyboardButton("🛠  Ajustes", callback_data="config"),
        InlineKeyboardButton("❓  Ayuda", callback_data="help"),
        InlineKeyboardButton("📊  Estado", callback_data="estado"),
    ])
    kb.append([
        InlineKeyboardButton("📜  Script", callback_data="script"),
        InlineKeyboardButton("🧹  Limpiar", callback_data="limpiar"),
    ])
    return InlineKeyboardMarkup(kb)


async def show_menu(chat_id, edit_msg=None):
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
    if edit_msg:
        await tg_edit(edit_msg, text, reply_markup=menu_keyboard(), force=True)
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
        "• `/estado` — Estado del bot (uptime, memoria).\n"
        "• `/config` — Ver y cambiar ajustes.\n"
        "• `/script` — Recibir `descargar_enlace.py`.\n"
        "• `/temp` — Listar archivos temporales.\n"
        "• `/limpiar` — Borrar archivos temporales.\n"
        "• `/renombrar <nombre>` — Renombrar último archivo.\n"
        "• `/recortar <bytes>` — Dejar solo los primeros N bytes.\n"
        "• `/zip` — Comprimir último archivo en `.zip`.\n"
        "• `/size` `/tamaño` — Tamaño del último archivo.\n\n"
        "✅ *Sin límite de tamaño:* este bot usa Pyrogram (MTProto), así "
        "que puede descargar archivos de cualquier tamaño desde Telegram.\n\n"
        "⚠️ *Límite de OJS:* máximo `19 MB` por archivo subido "
        "(se parten automáticamente).\n\n"
        "💡 *Tip:* si falla una subida, usa `/limpiar` y reintenta."
    )


def estado_text():
    uptime = int(time.time() - START_TIME)
    idle = int(time.time() - LAST_ACTIVITY)
    mem_mb = 0
    try:
        import resource
        mem_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mem_mb = mem_kb / 1024 if mem_kb > 1024 * 1024 else mem_kb
    except Exception:
        pass
    return (
        f"📊 *Estado del Bot* (Pyrogram)\n\n"
        f"• ⏱ *Uptime:* `{timedelta(seconds=uptime)}`\n"
        f"• 🕐 *Última actividad:* {idle}s atrás\n"
        f"• 💾 *Memoria:* `{mem_mb:.0f} MB`\n"
        f"• ☁️ *Nube por defecto:* `{JOURNAL_DEFAULT}`\n"
        f"• 📦 *PART_MB:* `{PART_MB}`\n"
        f"• 🔢 *Revistas compatibles:* `{len(JOURNAL_SLUGS)}`\n"
        f"• 🌐 *Modo:* `{'24/7 (IDLE_MIN=0)' if IDLE_MIN == 0 else f'idle {IDLE_MIN}min'}`\n"
        f"• ⚡ *MTProto:* `sí (sin límite de tamaño)`"
    )


# --- Descarga de Telegram vía MTProto (sin límite de 20 MB) ----------------


async def download_from_telegram(client, message, dest_path, nombre,
                                  status_msg):
    """Descarga un archivo de Telegram vía MTProto con barra de progreso.
    Soporta archivos de CUALQUIER tamaño."""
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
        await tg_edit(status_msg,
                      f"⬇️ Descargando de Telegram\n`{nombre[:40]}`\n\n"
                      f"{bar} {pct:.0f}%\n"
                      f"{human_size(current)} / {human_size(total)}\n"
                      f"⚡ {human_speed(speed)} | ETA {int(eta)}s")

    await client.download_media(message, file_name=dest_path,
                                progress=on_progress)
    return dest_path


# --- Upload a OJS por partes con barra de progreso --------------------------


async def upload_to_ojs(journal, filepath, original_name, chat_id,
                        status_msg):
    """Sube `filepath` a `journal` por partes. Devuelve (lines, n_parts, sub)."""
    host, _ = cargar_revista(journal)
    pw = OJS_PASS_REVCARD if journal == "revcardiologia" else OJS_PASS
    api = OJSApi(host, OJS_USER, pw, journal=journal)
    size = os.path.getsize(filepath)

    # 1. Login
    await tg_edit(status_msg, f"🔐 Logueando en *{journal}*...", force=True)
    try:
        api.login()
    except Exception as e:
        raise RuntimeError(f"login {journal}: {e}")

    # 2. Crear submission
    await tg_edit(status_msg,
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
                   _t0=part_t0, _msg=status_msg, _loop=loop,
                   _journal=journal):
                # Esta función se llama desde un thread (requests), no desde
                # el loop async. Usar run_coroutine_threadsafe.
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
                        tg_edit(_msg, text), _loop)
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
                await tg_edit(status_msg,
                              f"⚠️ Error en parte {idx}/{total_parts}: "
                              f"`{err}`\nContinuando...", force=True)
            try:
                os.remove(part_path)
            except OSError:
                pass
    elapsed = time.time() - t_start
    await tg_edit(status_msg,
                  f"✅ Subida completada en *{journal}*\n"
                  f"`{idx}` partes en `{int(elapsed)}s`", force=True)
    return lines, idx, sub


# --- Descarga de URL directa -----------------------------------------------


async def download_url_direct(url, dest_dir, status_msg):
    """Descarga URL directa con barra de progreso."""
    fname = url.rstrip("/").split("/")[-1] or "archivo.bin"
    fname = fname.split("?")[0]
    fname = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", fname)[:100]
    if not fname:
        fname = "archivo.bin"
    dest = os.path.join(dest_dir, fname)

    async def _download():
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
                now = time.time()
                if now - last_edit >= 1.0:
                    last_edit = now
                    elapsed = now - t0
                    speed = written / elapsed if elapsed > 0 else 0
                    pct = (written / total * 100) if total else 0
                    bar = progress_bar(written, total)
                    eta = ((total - written) / speed) if speed > 0 else 0
                    await tg_edit(status_msg,
                                  f"⬇️ Descargando de Internet\n"
                                  f"`{fname[:40]}`\n\n"
                                  f"{bar} {pct:.0f}%\n"
                                  f"{human_size(written)} / {human_size(total)}\n"
                                  f"⚡ {human_speed(speed)} | ETA {int(eta)}s")
        return dest

    return await _download()


# --- Procesar archivo recibido ----------------------------------------------


async def process_upload(client, m: Message):
    chat = m.chat.id

    # Detectar el archivo/media
    doc = m.document or m.video or m.audio or m.animation or m.voice or \
        m.video_note
    if not doc and m.photo:
        doc = m.photo[-1] if m.photo else None

    if not doc:
        await tg_send(chat, "❌ No detecté ningún archivo en el mensaje.")
        return

    # Obtener nombre y tamaño
    if hasattr(doc, "file_name") and doc.file_name:
        nombre = doc.file_name
    elif m.video:
        nombre = m.video.file_name or f"video_{int(time.time())}.mp4"
    elif m.audio:
        nombre = m.audio.file_name or f"audio_{int(time.time())}.mp3"
    elif m.photo:
        nombre = f"foto_{int(time.time())}.jpg"
    else:
        nombre = f"media_{int(time.time())}.bin"

    size = getattr(doc, "file_size", 0) or 0
    journal = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
    if journal not in REVISTAS_DICT:
        await tg_send(chat, f"❌ Revista no válida: `{journal}`. "
                            f"Usa /start para elegir.")
        return

    size_human = human_size(size)
    status = await tg_send(chat,
                           f"📥 Recibí `{nombre[:50]}` ({size_human}). "
                           f"Nube: *{journal}*")
    if not status:
        return

    safe_name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", nombre)[:100]
    local = f"/tmp/tg_{int(time.time())}_{safe_name}"

    # 1. Descargar de Telegram (Pyrogram soporta cualquier tamaño)
    try:
        await download_from_telegram(client, m, local, nombre, status)
    except Exception as e:
        await tg_send(chat, f"❌ Descarga de Telegram falló: "
                            f"`{str(e)[:200]}`")
        try:
            os.remove(local)
        except OSError:
            pass
        return

    actual_size = os.path.getsize(local)
    LAST_FILE[chat] = local

    # 2. Subir a OJS
    try:
        lines, n_parts, sub = await upload_to_ojs(
            journal, local, nombre, chat, status)
    except Exception as e:
        await tg_send(chat, f"❌ Error subiendo a *{journal}*:\n"
                            f"`{str(e)[:300]}`")
        return

    # 3. Generar .txt con enlaces
    txt = f"/tmp/enlaces_{Path(safe_name).stem}.txt"
    with open(txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    enlaces = [l for l in lines if not l.startswith("#")]
    errores = [l for l in lines if l.startswith("# ERROR")]
    if enlaces:
        PENDING_LINKS[str(chat)] = enlaces

    # 4. Botones
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋  Ver enlaces",
                              callback_data="ver_enlaces"),
         InlineKeyboardButton("📜  Script", callback_data="script")],
        [InlineKeyboardButton("☁️  Menú", callback_data="menu"),
         InlineKeyboardButton("🛠  Ajustes", callback_data="config")],
    ])

    caption = (f"✅ *{n_parts} partes* subidas a *{journal}*\n"
               f"📦 Submission: `{sub}`\n"
               f"📊 Tamaño: `{human_size(actual_size)}`")
    if errores:
        caption += f"\n⚠️ {len(errores)} parte(s) con error (ver .txt)"

    await tg_send_document(chat, txt, caption=caption)

    if len(enlaces) <= 4:
        enlace_text = "\n".join(f"`{e}`" for e in enlaces)
        await tg_send(chat, f"📋 *Enlaces generados:*\n\n{enlace_text}",
                      reply_markup=kb)
    else:
        await tg_send(chat, f"📋 *{len(enlaces)} enlaces* en el .txt adjunto "
                            f"arriba. Toca *Ver enlaces* para mostrarlos aquí.",
                      reply_markup=kb)


# --- Handlers de comandos ---------------------------------------------------


@app.on_message(filters.command(["start", "menu", "nube"]))
async def cmd_start(_, m: Message):
    await show_menu(m.chat.id)


@app.on_message(filters.command(["ayuda", "help"]))
async def cmd_help(_, m: Message):
    await tg_send(m.chat.id, help_text())


@app.on_message(filters.command(["estado"]))
async def cmd_estado(_, m: Message):
    await tg_send(m.chat.id, estado_text())


@app.on_message(filters.command(["script"]))
async def cmd_script(_, m: Message):
    script_path = HERE / "descargar_enlace.py"
    if script_path.exists():
        await tg_send_document(m.chat.id, str(script_path),
                               caption="📜 *descargar_enlace.py* para bajar "
                                       "los enlaces en tu PC.")
    else:
        await tg_send(m.chat.id, "❌ No encuentro `descargar_enlace.py`.")


@app.on_message(filters.command(["temp"]))
async def cmd_temp(_, m: Message):
    files = sorted(Path("/tmp").glob("tg_*"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:20]
    if not files:
        await tg_send(m.chat.id, "✅ No hay archivos temporales en `/tmp`.")
        return
    msg = "🧹 *Temporales* (últimos 20):\n\n"
    total = 0
    for p in files:
        sz = p.stat().st_size
        total += sz
        msg += f"• `{p.name}` ({human_size(sz)})\n"
    msg += f"\n📊 *Total:* {human_size(total)}\n\nUsa `/limpiar` para borrar."
    await tg_send(m.chat.id, msg)


@app.on_message(filters.command(["limpiar"]))
async def cmd_limpiar(_, m: Message):
    n, freed = 0, 0
    for p in Path("/tmp").glob("tg_*"):
        try:
            sz = p.stat().st_size
            p.unlink()
            n += 1
            freed += sz
        except OSError:
            pass
    await tg_send(m.chat.id, f"🧹 Limpié *{n}* archivos "
                             f"({human_size(freed)} liberados).")


@app.on_message(filters.command(["config"]))
async def cmd_config(_, m: Message):
    kb = [[InlineKeyboardButton(f"📡  {s}", callback_data=f"setdef:{s}")]
          for s in JOURNAL_SLUGS]
    kb.append([
        InlineKeyboardButton("➕ PART_MB +1", callback_data="partmb:1"),
        InlineKeyboardButton("➖ PART_MB -1", callback_data="partmb:-1"),
        InlineKeyboardButton("↩️ 5 MB", callback_data="partmb:5"),
    ])
    actual = CHOSEN.get(str(m.chat.id), JOURNAL_DEFAULT)
    await tg_send(m.chat.id,
                  f"⚙️ *Ajustes*\n\n"
                  f"• Revista por defecto: `{actual}`\n"
                  f"• PART_MB: `{PART_MB}` MB (límite OJS: 19 MB)\n"
                  f"• Idle: `{IDLE_MIN}` min\n"
                  f"• Bot: `@Uptimeriroobot` (Pyrogram)\n\n"
                  f"👇 Toca para cambiar:",
                  reply_markup=InlineKeyboardMarkup(kb))


@app.on_message(filters.command(["renombrar"]))
async def cmd_renombrar(_, m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await tg_send(m.chat.id, "Uso: `/renombrar nuevo_nombre.pdf`")
        return
    new_name = parts[1].strip()
    p = LAST_FILE.get(m.chat.id)
    if not p or not os.path.exists(p):
        await tg_send(m.chat.id, "❌ No hay archivo reciente. Envíame uno primero.")
        return
    new_path = os.path.join(os.path.dirname(p), new_name)
    try:
        os.rename(p, new_path)
        LAST_FILE[m.chat.id] = new_path
        await tg_send_document(m.chat.id, new_path,
                               caption=f"✅ Renombrado a `{new_name}`")
    except OSError as e:
        await tg_send(m.chat.id, f"❌ No pude renombrar: `{e}`")


@app.on_message(filters.command(["recortar"]))
async def cmd_recortar(_, m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await tg_send(m.chat.id, "Uso: `/recortar 1000000` "
                                 "(deja los primeros N bytes)")
        return
    n = int(parts[1].strip())
    p = LAST_FILE.get(m.chat.id)
    if not p or not os.path.exists(p):
        await tg_send(m.chat.id, "❌ No hay archivo reciente. Envíame uno primero.")
        return
    new_path = p + ".recortado"
    try:
        with open(p, "rb") as f, open(new_path, "wb") as g:
            g.write(f.read(n))
        LAST_FILE[m.chat.id] = new_path
        await tg_send_document(m.chat.id, new_path,
                               caption=f"✅ Recortado a `{human_size(n)}`")
    except OSError as e:
        await tg_send(m.chat.id, f"❌ No pude recortar: `{e}`")


@app.on_message(filters.command(["zip"]))
async def cmd_zip(_, m: Message):
    p = LAST_FILE.get(m.chat.id)
    if not p or not os.path.exists(p):
        await tg_send(m.chat.id, "❌ No hay archivo reciente. Envíame uno primero.")
        return
    zip_path = p + ".zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(p, os.path.basename(p))
        LAST_FILE[m.chat.id] = zip_path
        await tg_send_document(m.chat.id, zip_path,
                               caption=f"✅ Comprimido en `.zip` "
                                       f"({human_size(os.path.getsize(zip_path))})")
    except Exception as e:
        await tg_send(m.chat.id, f"❌ No pude comprimir: `{e}`")


@app.on_message(filters.command(["tamano", "tamaño", "size"]))
async def cmd_size(_, m: Message):
    p = LAST_FILE.get(m.chat.id)
    if not p or not os.path.exists(p):
        await tg_send(m.chat.id, "❌ No hay archivo reciente. Envíame uno primero.")
        return
    await tg_send(m.chat.id, f"📄 `{os.path.basename(p)}`\n"
                             f"📊 Tamaño: `{human_size(os.path.getsize(p))}`")


# --- Handler de texto (URLs o texto no reconocido) -------------------------


@app.on_message(filters.text & ~filters.command(
    ["start", "menu", "nube", "ayuda", "help", "estado", "script", "temp",
     "limpiar", "config", "renombrar", "recortar", "zip", "tamano",
     "tamaño", "size"]))
async def handle_text(client, m: Message):
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.time()
    text = (m.text or "").strip()
    if not text:
        return
    if not (text.startswith("http://") or text.startswith("https://")):
        await tg_send(m.chat.id,
                      "🤔 No reconocí ese mensaje.\n\n"
                      "Puedes:\n"
                      "• Enviarme un *archivo* (PDF, video, etc.).\n"
                      "• Enviarme un *enlace* http(s).\n"
                      "• Usar /start para el menú.\n"
                      "• Usar /ayuda para ver todos los comandos.")
        return

    status = await tg_send(m.chat.id, "🔗 Procesando enlace...")
    if not status:
        return
    try:
        local = await download_url_direct(text, "/tmp", status)
    except Exception as e:
        await tg_send(m.chat.id, f"❌ Descarga falló: `{str(e)[:200]}`")
        return
    if not local or not os.path.exists(local):
        await tg_send(m.chat.id, "❌ Descarga falló: archivo no encontrado.")
        return
    LAST_FILE[m.chat.id] = local
    size = os.path.getsize(local)
    await tg_send(m.chat.id, f"✅ Descargado: `{os.path.basename(local)}` "
                             f"({human_size(size)}). Subiendo a OJS...")
    journal = CHOSEN.get(str(m.chat.id), JOURNAL_DEFAULT)
    try:
        lines, n_parts, sub = await upload_to_ojs(
            journal, local, os.path.basename(local), m.chat.id, status)
    except Exception as e:
        await tg_send(m.chat.id, f"❌ Error subiendo: `{str(e)[:200]}`")
        return
    txt = f"/tmp/enlaces_{Path(local).stem}.txt"
    with open(txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    enlaces = [l for l in lines if not l.startswith("#")]
    if enlaces:
        PENDING_LINKS[str(m.chat.id)] = enlaces
    await tg_send_document(m.chat.id, txt,
                           caption=f"✅ {n_parts} partes en *{journal}* "
                                   f"(sub `{sub}`)")


# --- Handler de archivos/media ---------------------------------------------


@app.on_message(filters.document | filters.video | filters.audio |
                filters.animation | filters.voice | filters.video_note |
                filters.photo)
async def handle_media(client, m: Message):
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.time()
    await process_upload(client, m)


# --- Handler de callback queries (botones inline) --------------------------


@app.on_callback_query()
async def handle_callback(client, c: CallbackQuery):
    global PART_MB, LAST_ACTIVITY
    LAST_ACTIVITY = time.time()
    chat = c.message.chat.id
    msg = c.message
    data = c.data or ""

    # Responder siempre al callback para que el botón deje de "cargar"
    try:
        await c.answer()
    except Exception:
        pass

    if data == "help":
        await tg_send(chat, help_text())
        return
    if data == "estado":
        await tg_send(chat, estado_text())
        return
    if data == "menu":
        await show_menu(chat, edit_msg=msg)
        return
    if data == "config":
        kb = [[InlineKeyboardButton(f"📡  {s}",
                                    callback_data=f"setdef:{s}")]
              for s in JOURNAL_SLUGS]
        kb.append([
            InlineKeyboardButton("➕ PART_MB +1", callback_data="partmb:1"),
            InlineKeyboardButton("➖ PART_MB -1", callback_data="partmb:-1"),
            InlineKeyboardButton("↩️ 5 MB", callback_data="partmb:5"),
        ])
        actual = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
        await tg_edit(msg,
                      f"⚙️ *Ajustes*\n\n"
                      f"• Revista: `{actual}`\n"
                      f"• PART_MB: `{PART_MB}` MB\n"
                      f"• Idle: `{IDLE_MIN}` min\n\n"
                      f"👇 Toca para cambiar:",
                      reply_markup=InlineKeyboardMarkup(kb), force=True)
        return
    if data == "script":
        script_path = HERE / "descargar_enlace.py"
        if script_path.exists():
            await tg_send_document(chat, str(script_path),
                                   caption="📜 *descargar_enlace.py*")
        else:
            await tg_send(chat, "❌ No encuentro `descargar_enlace.py`.")
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
        await tg_edit(msg, f"🧹 Limpié *{n}* archivos "
                           f"({human_size(freed)} liberados).", force=True)
        return
    if data == "ver_enlaces":
        enlaces = PENDING_LINKS.get(str(chat), [])
        if not enlaces:
            await tg_send(chat, "❌ No hay enlaces recientes. "
                                "Sube un archivo primero.")
            return
        if len(enlaces) <= 4:
            text = "\n".join(f"`{e}`" for e in enlaces)
        else:
            text = "\n".join(f"`{e}`" for e in enlaces[:8])
            text += f"\n\n... y {len(enlaces)-8} más."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "📋  Copiar", callback_data="copiar_enlaces")]])
        await tg_send(chat, f"📋 *Enlaces:*\n\n{text}", reply_markup=kb)
        return
    if data == "copiar_enlaces":
        enlaces = PENDING_LINKS.get(str(chat), [])
        if enlaces:
            text = "\n".join(enlaces)
            for i in range(0, len(text), 4000):
                await tg_send(chat, f"```\n{text[i:i+4000]}\n```",
                              parse_mode="Markdown")
        return

    if data.startswith("nube:"):
        slug = data.split(":", 1)[1]
        if slug not in REVISTAS_DICT:
            await c.answer("Revista no válida", show_alert=True)
            return
        persist_chosen(chat, slug)
        await c.answer(f"✅ Nube: {slug}")
        await tg_edit(msg, f"✅ *Nube elegida:* `{slug}`\n\n"
                           f"Ahora envíame el archivo que quieres subir.",
                      force=True)
        return

    if data.startswith("setdef:"):
        slug = data.split(":", 1)[1]
        if slug not in REVISTAS_DICT:
            await c.answer("Revista no válida", show_alert=True)
            return
        persist_chosen(chat, slug)
        await c.answer(f"✅ Por defecto: {slug}")
        await tg_edit(msg, f"✅ *Revista por defecto:* `{slug}`", force=True)
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
        await c.answer(f"✅ PART_MB = {PART_MB} MB")
        await tg_edit(msg, f"✅ *PART_MB = `{PART_MB}` MB*\n"
                           f"(antes: {old} MB, límite OJS: {MAX_PART_MB} MB)",
                      force=True)
        return

    await c.answer("Acción no reconocida", show_alert=True)


# --- Refresca actividad en grupo 1 ----------------------------------------


@app.on_message(group=1)
async def _bump_activity(_, m: Message):
    """Refresca el timestamp de actividad (para idle timeout)."""
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.time()


# --- Idle watcher (cierra si IDLE_MIN > 0 y hay inactividad) --------------


async def idle_watcher():
    while True:
        await asyncio.sleep(60)
        if IDLE_MIN > 0 and time.time() - LAST_ACTIVITY > IDLE_MIN * 60:
            log.info("Sin actividad por %d min, cerrando.", IDLE_MIN)
            await app.stop()
            return


# --- Mini servidor HTTP (para Render health check) ------------------------


def _run_http_server(port):
    """Función bloqueante que arranca un HTTP server en un thread.
    Render free requiere responder a / para no matar el proceso."""
    import socketserver
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path in ("/", "/health", "/ping"):
                bot_alive = app.is_initialized if 'app' in globals() else False
                body = (f"OK\nbot_alive={bot_alive}\n"
                        f"pending_links_chats={len(PENDING_LINKS)}\n"
                        f"logs={len(LOG_BUFFER)}\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/logs":
                # Devolver últimos logs en JSON
                body = json.dumps(list(LOG_BUFFER), ensure_ascii=False,
                                  indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/status":
                body = json.dumps({
                    "bot_alive": app.is_initialized if 'app' in globals() else False,
                    "uptime_s": int(time.time() - START_TIME),
                    "last_activity_s": int(time.time() - LAST_ACTIVITY),
                    "pending_links_chats": len(PENDING_LINKS),
                    "chosen": CHOSEN,
                    "log_buffer_size": len(LOG_BUFFER),
                    "recent_logs": list(LOG_BUFFER)[-10:],
                }, ensure_ascii=False, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
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
    """Arranca el HTTP server en un thread daemon si la variable PORT existe."""
    import threading
    port = int(os.environ.get("PORT", "0"))
    if not port:
        return  # no hay PORT, no estamos en Render
    t = threading.Thread(target=_run_http_server, args=(port,),
                         daemon=True, name="http")
    t.start()


# --- Main ------------------------------------------------------------------


# --- Polling HTTP manual para bots (Pyrogram no lo hace solo) --------------
# Pyrogram 2.x en modo bot NO hace getUpdates automáticamente. Tenemos que
# hacer polling HTTP manual y alimentar los updates al dispatcher de Pyrogram.


async def http_polling_loop():
    """Hace long polling HTTP a getUpdates y pasa los updates a Pyrogram.
    Esto es necesario porque Pyrogram espera updates push vía MTProto,
    pero Telegram no los envía a bots (los bots deben hacer getUpdates)."""
    import requests as _r
    API_HTTP = f"https://api.telegram.org/bot{TOKEN}"
    offset = 0
    log.info(">>> Iniciando polling HTTP de getUpdates...")
    while True:
        try:
            r = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _r.get(f"{API_HTTP}/getUpdates",
                               params={"timeout": 25, "offset": offset,
                                       "allowed_updates": json.dumps(
                                           ["message", "edited_message",
                                            "callback_query"])},
                               timeout=60))
            data = r.json()
            if not data.get("ok"):
                log.warning("getUpdates error: %s", data)
                await asyncio.sleep(5)
                continue
            for up in data.get("result", []):
                offset = up["update_id"] + 1
                # Pasar el update a Pyrogram para que los handlers se ejecuten
                try:
                    await app.handle_updates([up])
                except Exception as e:
                    log.warning("handle_updates error: %s", e)
        except Exception as e:
            log.warning("polling loop error: %s", e)
            await asyncio.sleep(5)


# --- Main ------------------------------------------------------------------


async def main_async():
    """Arranca el bot con Pyrogram + polling HTTP manual."""
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.time()

    # Arrancar HTTP server en thread daemon (para Render health check)
    start_http_server_if_needed()

    log.info(">>> Iniciando app.start() con Pyrogram...")
    try:
        await app.start()
    except Exception as e:
        log.error(">>> FALLO app.start(): %s", e)
        log.error(">>> Traceback: %s", traceback.format_exc())
        while True:
            await asyncio.sleep(60)
            log.error(">>> Bot caído: %s", e)

    try:
        me = await app.get_me()
        log.info("Bot iniciado como @%s (id=%s) con Pyrogram + MTProto",
                 me.username, me.id)
    except Exception as e:
        log.error(">>> FALLO get_me(): %s", e)
        while True:
            await asyncio.sleep(60)

    log.info("API_ID=%s, PART_MB=%d, JOURNAL_DEFAULT=%s, IDLE_MIN=%d",
             API_ID, PART_MB, JOURNAL_DEFAULT, IDLE_MIN)

    # Arrancar el loop de polling HTTP en background
    asyncio.create_task(http_polling_loop())
    asyncio.create_task(idle_watcher())

    # Mantener vivo el loop async
    while True:
        await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("Interrumpido por usuario.")
