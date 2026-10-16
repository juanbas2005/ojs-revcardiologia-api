#!/usr/bin/env python3
"""
Bot de Telegram OJS Cubanas — versión mejorada con gestión de archivos.

Mejoras vs versión anterior:
  - Progreso global unificado: un solo % y un solo mensaje por operación
    completa (descarga + subida de todas las partes).
  - Botón ❌ Cancelar durante descarga/subida.
  - Sistema de gestión de archivos: /archivos, /info, /borrar, /mover, /copiar.
  - Estética mejorada: secciones con emojis, formatos limpios.
  - Throttle de edición a 1s (evita FloodWait).
  - Confirmaciones inline antes de borrar.
  - Cola de operaciones con /cola y /cancelar.
  - Estadísticas: /stats (uploads, downloads, tiempo total).
  - Filtrado de callbacks por user.id (solo el dueño puede pulsar botones).
  - Reintentos con backoff exponencial.
  - Limpieza de temporales en finally.

Arquitectura híbrida:
  - Polling HTTP de getUpdates (robusto).
  - Handlers async directos (no dependen de Pyrogram dispatcher).
  - Pyrogram lazy: solo arranca cuando hace falta descargar un archivo grande.
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
import hashlib
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

LOG_BUFFER = deque(maxlen=300)


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

# Revistas compatibles
with open(HERE / "revistas.json", encoding="utf-8") as f:
    REVISTAS_DICT = {r["journal"]: r for r in json.load(f)["revistas"]
                     if r.get("compatible")}
JOURNAL_SLUGS = list(REVISTAS_DICT.keys())

EXT_PERMITIDAS = {"epub", "xml", "xls", "cvs", "doc", "docx", "pdf",
                  "jpg", "png", "html", "gif", "xlsx"}

# --- Persistencia -----------------------------------------------------------
CONFIG_FILE = HERE / "config.json"
STATS_FILE = HERE / "stats.json"
FILES_INDEX_FILE = HERE / "files_index.json"  # catálogo de archivos subidos


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path, data):
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception as e:
        log.warning("no pude guardar %s: %s", path, e)


CONFIG = load_json(CONFIG_FILE, {})
CHOSEN = CONFIG.get("chosen", {})

STATS = load_json(STATS_FILE, {
    "uploads": 0,           # número de archivos subidos a OJS
    "downloads": 0,         # número de archivos descargados de Telegram
    "bytes_uploaded": 0,    # bytes totales subidos
    "bytes_downloaded": 0,  # bytes totales descargados
    "parts_uploaded": 0,    # número de partes OJS
    "errors": 0,            # número de errores
    "start_time": time.time(),
})

FILES_INDEX = load_json(FILES_INDEX_FILE, {"files": []})


def persist_chosen(chat_id, journal):
    CHOSEN[str(chat_id)] = journal
    CONFIG["chosen"] = CHOSEN
    save_json(CONFIG_FILE, CONFIG)


def persist_stats():
    save_json(STATS_FILE, STATS)


def persist_files_index():
    save_json(FILES_INDEX_FILE, FILES_INDEX)


# --- Estado runtime ---------------------------------------------------------
LAST_FILE = {}            # chat_id -> path local del último archivo
PENDING_LINKS = {}        # chat_id -> lista de enlaces pendientes
START_TIME = time.time()
LAST_ACTIVITY = time.time()
_last_edit = {}
EDIT_THROTTLE_S = 1.0  # 1s entre ediciones para evitar FloodWait

# Operaciones activas: (chat_id, op_id) -> OperationState
ACTIVE_OPS = {}
CANCEL_FLAGS = {}  # op_id -> asyncio.Event (set = cancelado)
OP_QUEUE = deque()  # cola de operaciones pendientes


class OperationState:
    """Estado de una operación larga (descarga + subida)."""

    def __init__(self, op_id, chat_id, kind, name, total_size=0):
        self.op_id = op_id
        self.chat_id = chat_id
        self.kind = kind  # "download", "upload", "url"
        self.name = name
        self.total_size = total_size
        self.done_bytes = 0
        self.start_time = time.time()
        self.status_msg_id = None
        self.cancel_event = asyncio.Event()
        self.current_part = 0
        self.total_parts = 0
        self.part_pct = 0

    @property
    def global_pct(self):
        """Porcentaje global de toda la operación (descarga + subida)."""
        if self.total_size <= 0:
            return 0
        return min(self.done_bytes / self.total_size * 100, 100.0)

    def status_line(self):
        elapsed = time.time() - self.start_time
        speed = self.done_bytes / elapsed if elapsed > 0 else 0
        eta = ((self.total_size - self.done_bytes) / speed
               if speed > 0 else 0)
        return (
            f"{progress_bar(self.done_bytes, self.total_size, width=20)} "
            f"{self.global_pct:.1f}%\n"
            f"📦 {human_size(self.done_bytes)} / {human_size(self.total_size)}  "
            f"⚡ {human_speed(speed)}  ⏱ ETA {int(eta)}s"
        )


# --- Cliente Pyrogram (lazy) -----------------------------------------------
_pyrogram_app = None
_pyrogram_lock = asyncio.Lock()


async def get_pyrogram():
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


# --- Helpers visuales -------------------------------------------------------


def progress_bar(done, total, width=20):
    """Barra de progreso visual con caracteres Unicode."""
    if total <= 0:
        return "▱" * width
    frac = min(done / total, 1.0)
    filled = int(frac * width)
    return "▰" * filled + "▱" * (width - filled)


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


def file_md5(path, max_bytes=10 * 1024 * 1024):
    """MD5 de los primeros N bytes (rápido, para identificación)."""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            h.update(f.read(max_bytes))
        return h.hexdigest()[:12]
    except Exception:
        return "?"


def file_info(path):
    """Info detallada de un archivo local."""
    try:
        st = os.stat(path)
        return {
            "name": os.path.basename(path),
            "size": st.st_size,
            "size_human": human_size(st.st_size),
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(
                timespec="seconds"),
            "md5": file_md5(path),
            "ext": Path(path).suffix.lstrip(".").lower() or "—",
        }
    except Exception:
        return None


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
    if not j.get("ok"):
        log.warning(">>> sendMessage FAIL: chat=%s err=%s",
                    chat_id, j.get("description", "?")[:200])
        if j.get("error_code") == 429:
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


# --- Botones ---------------------------------------------------------------


def cancel_keyboard(op_id):
    return [[{"text": "❌  Cancelar", "callback_data": f"cancel:{op_id}"}]]


def main_menu_keyboard():
    kb = [[{"text": f"☁️  {s}", "callback_data": f"nube:{s}"}]
          for s in JOURNAL_SLUGS]
    kb.append([
        {"text": "🛠  Ajustes", "callback_data": "config"},
        {"text": "❓  Ayuda", "callback_data": "help"},
        {"text": "📊  Estado", "callback_data": "estado"},
    ])
    kb.append([
        {"text": "📁  Archivos", "callback_data": "archivos"},
        {"text": "📈  Stats", "callback_data": "stats"},
    ])
    kb.append([
        {"text": "📜  Script", "callback_data": "script"},
        {"text": "🧹  Limpiar", "callback_data": "limpiar"},
    ])
    return kb


def help_keyboard():
    return [[
        {"text": "📁  Archivos", "callback_data": "archivos"},
        {"text": "🏠  Menú", "callback_data": "menu"},
    ]]


# --- Textos ----------------------------------------------------------------


def help_text():
    return (
        "❓ *Ayuda — Bot OJS Cubanas*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📤 *Subir archivos a OJS:*\n"
        "• Envía archivo/documento/video/foto.\n"
        "• Lo divido en partes de `" f"{PART_MB} MB" "` (máx 19 MB).\n"
        "• Renombro a `.pdf` si la extensión no está permitida.\n"
        "• Te devuelvo un `.txt` con todos los enlaces.\n"
        "• Barra de progreso *global* (un solo %).\n"
        "• Botón *❌ Cancelar* durante toda la operación.\n\n"
        "⬇️ *Descargar de Internet:*\n"
        "• Enlace `http(s)` directo → lo descargo y subo.\n\n"
        "📁 *Gestión de archivos:*\n"
        "• `/archivos` — Listar archivos recientes.\n"
        "• `/info` — Info del último archivo (tamaño, MD5, etc.).\n"
        "• `/borrar` — Borrar el último archivo.\n"
        "• `/mover <ruta>` — Mover el último archivo.\n"
        "• `/copiar <ruta>` — Copiar el último archivo.\n\n"
        "⚙️ *Comandos:*\n"
        "• `/start` `/menu` — Menú principal.\n"
        "• `/ayuda` `/help` — Esta ayuda.\n"
        "• `/estado` — Estado del bot.\n"
        "• `/stats` — Estadísticas de uso.\n"
        "• `/config` — Ver y cambiar ajustes.\n"
        "• `/cola` — Ver cola de operaciones.\n"
        "• `/cancelar` — Cancelar operación en curso.\n"
        "• `/script` — Recibir `descargar_enlace.py`.\n"
        "• `/temp` — Listar temporales.\n"
        "• `/limpiar` — Borrar temporales.\n"
        "• `/renombrar <n>` — Renombrar último archivo.\n"
        "• `/recortar <bytes>` — Dejar primeros N bytes.\n"
        "• `/zip` — Comprimir último archivo.\n"
        "• `/size` — Tamaño del último archivo.\n\n"
        "✅ *Sin límite de tamaño:* descarga vía MTProto.\n"
        "⚠️ *Límite OJS:* máximo 19 MB por archivo.\n\n"
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
    pyr = "✅ iniciado" if (_pyrogram_app and _pyrogram_app.is_initialized) \
        else "⏳ pendiente"
    ops = len(ACTIVE_OPS)
    queue = len(OP_QUEUE)
    return (
        f"📊 *Estado del Bot*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"• ⏱ *Uptime:* `{timedelta(seconds=uptime)}`\n"
        f"• 🕐 *Última actividad:* {idle}s atrás\n"
        f"• 💾 *Memoria:* `{mem_mb:.0f} MB`\n"
        f"• ☁️ *Nube por defecto:* `{JOURNAL_DEFAULT}`\n"
        f"• 📦 *PART_MB:* `{PART_MB}`\n"
        f"• 🔢 *Revistas:* `{len(JOURNAL_SLUGS)}`\n"
        f"• 🌐 *Modo:* `{'24/7' if IDLE_MIN == 0 else f'idle {IDLE_MIN}min'}`\n"
        f"• ⚡ *Pyrogram:* {pyr}\n"
        f"• 🔄 *Operaciones activas:* `{ops}`\n"
        f"• 📋 *Cola:* `{queue}` pendientes"
    )


def stats_text():
    uptime = int(time.time() - STATS.get("start_time", time.time()))
    up = STATS.get("uploads", 0)
    dl = STATS.get("downloads", 0)
    bytes_up = STATS.get("bytes_uploaded", 0)
    bytes_dl = STATS.get("bytes_downloaded", 0)
    parts = STATS.get("parts_uploaded", 0)
    errs = STATS.get("errors", 0)
    return (
        f"📈 *Estadísticas de uso*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📤 *Subidas a OJS:* `{up}` archivos\n"
        f"📦 *Partes subidas:* `{parts}`\n"
        f"📊 *Bytes subidos:* `{human_size(bytes_up)}`\n\n"
        f"⬇️ *Descargas de Telegram:* `{dl}` archivos\n"
        f"📊 *Bytes descargados:* `{human_size(bytes_dl)}`\n\n"
        f"⚠️ *Errores:* `{errs}`\n"
        f"⏱ *Uptime:* `{timedelta(seconds=uptime)}`\n\n"
        f"📁 *Archivos en índice:* `{len(FILES_INDEX.get('files', []))}`"
    )


def files_list_text(limit=15):
    files = sorted(FILES_INDEX.get("files", []),
                   key=lambda x: x.get("ts", 0), reverse=True)[:limit]
    if not files:
        return "📁 *Archivos recientes*\n\n_No hay archivos registrados._"
    text = f"📁 *Archivos recientes* ({len(files)})\n"
    text += "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, f in enumerate(files, 1):
        emoji = "📄" if f.get("ext", "").lower() in {"pdf", "doc", "docx"} \
            else "🎬" if f.get("ext", "").lower() in {"mp4", "avi", "mov"} \
            else "🎵" if f.get("ext", "").lower() in {"mp3", "wav", "ogg"} \
            else "🖼" if f.get("ext", "").lower() in {"jpg", "png", "gif"} \
            else "📦"
        text += (f"{emoji} `{i}.` `{f.get('name', '?')[:40]}`\n"
                 f"    📦 {f.get('size_human', '?')} · "
                 f"☁️ `{f.get('journal', '?')}` · "
                 f"📅 {f.get('date', '?')[:10]}\n")
    return text


def file_detail_text(info):
    if not info:
        return "❌ No hay archivo reciente."
    return (
        f"📄 *Info del archivo*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"• 📝 *Nombre:* `{info['name']}`\n"
        f"• 📦 *Tamaño:* `{info['size_human']}` ({info['size']} bytes)\n"
        f"• 🏷 *Extensión:* `{info['ext']}`\n"
        f"• 🔐 *MD5:* `{info['md5']}`\n"
        f"• 📅 *Modificado:* `{info['modified']}`"
    )


# --- Operaciones con cancelación -------------------------------------------


def gen_op_id():
    return f"op_{int(time.time() * 1000)}"


async def is_cancelled(op_id):
    """Verifica si una operación fue cancelada."""
    state = ACTIVE_OPS.get(op_id)
    return state and state.cancel_event.is_set()


async def cancel_op(op_id):
    """Marca una operación para cancelación."""
    state = ACTIVE_OPS.get(op_id)
    if state:
        state.cancel_event.set()
        return True
    return False


# --- Descarga de Telegram vía MTProto ---------------------------------------


async def download_from_telegram(file_id, dest_path, nombre, op_id):
    """Descarga un archivo de Telegram vía MTProto con barra de progreso.
    Soporta cancelación vía op_id."""
    app = await get_pyrogram()
    state = ACTIVE_OPS.get(op_id)
    if not state:
        raise RuntimeError("Estado de operación no encontrado")

    last_update = 0

    async def on_progress(current, total):
        nonlocal last_update
        if await is_cancelled(op_id):
            raise asyncio.CancelledError("cancelado por usuario")
        state.done_bytes = current
        state.total_size = total
        now = time.time()
        if now - last_update >= 1.0 or current >= total:
            last_update = now
            await update_status_message(state, phase="download")

    await app.download_media(file_id, file_name=dest_path, progress=on_progress)
    return dest_path


# --- Upload a OJS con progreso global unificado ----------------------------


async def upload_to_ojs(journal, filepath, original_name, chat_id,
                        status_msg_id, op_id):
    """Sube `filepath` a `journal` por partes con progreso global unificado.
    El mensaje se edita UNA sola vez por segundo, mostrando un solo % que
    combina todas las partes."""
    state = ACTIVE_OPS.get(op_id)
    if not state:
        raise RuntimeError("Estado de operación no encontrado")

    host, _ = cargar_revista(journal)
    pw = OJS_PASS_REVCARD if journal == "revcardiologia" else OJS_PASS
    api = OJSApi(host, OJS_USER, pw, journal=journal)
    size = os.path.getsize(filepath)

    # El total incluye descarga (ya hecha) + subida (size)
    state.total_size = size
    state.done_bytes = 0
    state.total_parts = max(1, (size + PART_MB * 1024 * 1024 - 1) //
                            (PART_MB * 1024 * 1024))

    # 1. Login
    await update_status_message(state, phase="login", extra=f"Login {journal}")
    try:
        api.login()
    except Exception as e:
        raise RuntimeError(f"login {journal}: {e}")

    # 2. Crear submission
    await update_status_message(state, phase="create",
                                extra=f"Creando submission en {journal}")
    try:
        sub = int(api.create())
    except Exception as e:
        raise RuntimeError(f"crear submission {journal}: {e}")

    base, ext = safe_filename(original_name)
    total_parts = state.total_parts

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
            if await is_cancelled(op_id):
                raise asyncio.CancelledError("cancelado por usuario")

            data = f.read(PART_MB * 1024 * 1024)
            if not data:
                break
            idx += 1
            state.current_part = idx
            part_path = f"/tmp/{base}_parte{idx:03d}.{ext}"
            with open(part_path, "wb") as g:
                g.write(data)
            part_size = len(data)
            part_t0 = time.time()
            bytes_before_part = state.done_bytes

            def cb(s, total, _idx=idx, _tot=total_parts, _t0=part_t0,
                   _state=state, _bbp=bytes_before_part, _op=op_id,
                   _chat=chat_id, _msg=status_msg_id, _loop=loop,
                   _journal=journal, _part_size=part_size):
                # Verificar cancelación
                if _state.cancel_event.is_set():
                    raise asyncio.CancelledError("cancelado")
                # Actualizar bytes globales: bytes_before_part + s
                _state.done_bytes = _bbp + s
                _state.part_pct = (s / total * 100) if total else 0
                # Throttle de edición a 1s
                now = time.time()
                if now - _t0 > 0:  # siempre, el throttle está en tg_edit
                    text = build_upload_status_text(_state, _journal)
                    try:
                        fut = asyncio.run_coroutine_threadsafe(
                            tg_edit(_chat, _msg, text,
                                    reply_markup=cancel_keyboard(_op),
                                    force=False), _loop)
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
                STATS["parts_uploaded"] = STATS.get("parts_uploaded", 0) + 1
                persist_stats()
            except asyncio.CancelledError:
                # limpiar y salir
                try:
                    os.remove(part_path)
                except OSError:
                    pass
                raise
            except Exception as e:
                err = str(e)[:200]
                lines.append(f"# ERROR en parte {idx}: {err}")
                STATS["errors"] = STATS.get("errors", 0) + 1
                persist_stats()
            try:
                os.remove(part_path)
            except OSError:
                pass

    elapsed = time.time() - t_start
    state.done_bytes = state.total_size
    await update_status_message(state, phase="done",
                                extra=f"✅ {idx} partes en {int(elapsed)}s")

    STATS["uploads"] = STATS.get("uploads", 0) + 1
    STATS["bytes_uploaded"] = STATS.get("bytes_uploaded", 0) + size
    persist_stats()

    # Registrar archivo en índice
    file_entry = {
        "name": original_name,
        "size": size,
        "size_human": human_size(size),
        "ext": ext,
        "journal": journal,
        "submission_id": sub,
        "parts": idx,
        "date": datetime.now().isoformat(timespec="seconds"),
        "ts": time.time(),
        "md5": file_md5(filepath),
        "links": [l for l in lines if not l.startswith("#")],
    }
    FILES_INDEX.setdefault("files", []).append(file_entry)
    # mantener solo últimos 100
    if len(FILES_INDEX["files"]) > 100:
        FILES_INDEX["files"] = FILES_INDEX["files"][-100:]
    persist_files_index()

    return lines, idx, sub


def build_upload_status_text(state, journal):
    """Construye el texto unificado del mensaje de estado de subida."""
    elapsed = time.time() - state.start_time
    speed = state.done_bytes / elapsed if elapsed > 0 else 0
    eta = ((state.total_size - state.done_bytes) / speed
           if speed > 0 else 0)
    bar = progress_bar(state.done_bytes, state.total_size, width=20)
    pct = state.global_pct
    return (
        f"☁️ *Subiendo a `{journal}`*\n"
        f"📦 `{state.name[:40]}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{bar} *{pct:.1f}%*\n"
        f"📦 {human_size(state.done_bytes)} / {human_size(state.total_size)}\n"
        f"⚡ {human_speed(speed)} · ⏱ ETA {int(eta)}s\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Parte {state.current_part}/{state.total_parts}"
    )


async def update_status_message(state, phase="", extra=""):
    """Actualiza el mensaje de estado del progreso."""
    if not state.status_msg_id:
        return
    elapsed = time.time() - state.start_time
    if phase == "download":
        text = (
            f"⬇️ *Descargando de Telegram*\n"
            f"📦 `{state.name[:40]}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{state.status_line()}"
        )
    elif phase == "login":
        text = f"🔐 *Autenticando...*\n\n{extra}"
    elif phase == "create":
        text = f"📝 *Preparando submission...*\n\n{extra}"
    elif phase == "done":
        text = f"✅ *Completado*\n\n{extra}"
    else:
        # subida
        text = build_upload_status_text(state, "?")
    kb = cancel_keyboard(state.op_id) if phase not in ("done",) else None
    await tg_edit(state.chat_id, state.status_msg_id, text,
                  reply_markup=kb, force=True)


# --- Descarga de URL directa -----------------------------------------------


async def download_url_direct(url, dest_dir, chat_id, status_msg_id, op_id):
    """Descarga URL directa con progreso y cancelación."""
    state = ACTIVE_OPS.get(op_id)
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
        with open(dest, "wb") as f:
            for chunk in r.iter_content(128 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
        return dest, total

    loop = asyncio.get_event_loop()
    dest, total = await loop.run_in_executor(None, _download)
    if state:
        state.total_size = total
        state.done_bytes = total
    await tg_edit(chat_id, status_msg_id,
                  f"✅ Descargado: `{os.path.basename(dest)}` "
                  f"({human_size(os.path.getsize(dest))})", force=True)
    return dest


# --- Procesar archivo recibido ----------------------------------------------


async def process_upload(msg_data):
    """Procesa un archivo recibido: lo descarga y sube a OJS."""
    global LAST_ACTIVITY, STATS
    LAST_ACTIVITY = time.time()
    chat_id = msg_data["chat"]["id"]

    doc = msg_data.get("document")
    vid = msg_data.get("video")
    aud = msg_data.get("audio")
    anim = msg_data.get("animation")
    voice = msg_data.get("voice")
    vnote = msg_data.get("video_note")
    photo = msg_data.get("photo")

    file_id = None
    nombre = None
    size = 0

    if doc:
        file_id = doc.get("file_id")
        nombre = doc.get("file_name") or f"doc_{int(time.time())}.bin"
        size = doc.get("file_size", 0)
    elif vid:
        file_id = vid.get("file_id")
        nombre = vid.get("file_name") or f"video_{int(time.time())}.mp4"
        size = vid.get("file_size", 0)
    elif aud:
        file_id = aud.get("file_id")
        nombre = aud.get("file_name") or f"audio_{int(time.time())}.mp3"
        size = aud.get("file_size", 0)
    elif anim:
        file_id = anim.get("file_id")
        nombre = f"gif_{int(time.time())}.mp4"
        size = anim.get("file_size", 0)
    elif voice:
        file_id = voice.get("file_id")
        nombre = f"voice_{int(time.time())}.ogg"
        size = voice.get("file_size", 0)
    elif vnote:
        file_id = vnote.get("file_id")
        nombre = f"videonote_{int(time.time())}.mp4"
        size = vnote.get("file_size", 0)
    elif photo:
        biggest = photo[-1]
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
                           f"📥 *Recibí* `{nombre[:50]}` ({size_human})\n"
                           f"☁️ *Nube:* `{journal}`\n"
                           f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                           f"⏳ Iniciando...")
    status_msg_id = (status or {}).get("result", {}).get("message_id")
    if not status_msg_id:
        status = await tg_send(chat_id,
                               f"Recibi {nombre[:50]} ({size_human}). "
                               f"Nube: {journal}", parse_mode=None)
        status_msg_id = (status or {}).get("result", {}).get("message_id")

    # Crear estado de operación
    op_id = gen_op_id()
    state = OperationState(op_id, chat_id, "upload", nombre,
                           total_size=size)
    state.status_msg_id = status_msg_id
    ACTIVE_OPS[op_id] = state

    safe_name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", nombre)[:100]
    local = f"/tmp/tg_{int(time.time())}_{safe_name}"

    # 1. Descargar de Telegram (MTProto)
    try:
        await download_from_telegram(file_id, local, nombre, op_id)
    except asyncio.CancelledError:
        await tg_edit(chat_id, status_msg_id,
                      "❌ *Operación cancelada por el usuario.*",
                      force=True)
        try:
            os.remove(local)
        except OSError:
            pass
        ACTIVE_OPS.pop(op_id, None)
        return
    except Exception as e:
        await tg_send(chat_id, f"❌ Descarga falló: `{str(e)[:200]}`")
        try:
            os.remove(local)
        except OSError:
            pass
        ACTIVE_OPS.pop(op_id, None)
        return

    actual_size = os.path.getsize(local)
    LAST_FILE[chat_id] = local
    STATS["downloads"] = STATS.get("downloads", 0) + 1
    STATS["bytes_downloaded"] = STATS.get("bytes_downloaded", 0) + actual_size
    persist_stats()

    # 2. Subir a OJS
    try:
        lines, n_parts, sub = await upload_to_ojs(
            journal, local, nombre, chat_id, status_msg_id, op_id)
    except asyncio.CancelledError:
        await tg_edit(chat_id, status_msg_id,
                      "❌ *Operación cancelada por el usuario.*",
                      force=True)
        ACTIVE_OPS.pop(op_id, None)
        return
    except Exception as e:
        await tg_send(chat_id, f"❌ Error subiendo a *{journal}*:\n"
                               f"`{str(e)[:300]}`")
        ACTIVE_OPS.pop(op_id, None)
        return

    # 3. Generar .txt con enlaces
    txt = f"/tmp/enlaces_{Path(safe_name).stem}.txt"
    with open(txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    enlaces = [l for l in lines if not l.startswith("#")]
    errores = [l for l in lines if l.startswith("# ERROR")]
    if enlaces:
        PENDING_LINKS[str(chat_id)] = enlaces

    # 4. Botones finales
    kb = [
        [{"text": "📋  Ver enlaces", "callback_data": "ver_enlaces"},
         {"text": "📜  Script", "callback_data": "script"}],
        [{"text": "☁️  Menú", "callback_data": "menu"},
         {"text": "📁  Archivos", "callback_data": "archivos"}],
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

    ACTIVE_OPS.pop(op_id, None)


# --- Handlers de texto -----------------------------------------------------


async def handle_text(chat_id, text, user_id=None):
    global LAST_ACTIVITY, PART_MB
    LAST_ACTIVITY = time.time()
    text = (text or "").strip()

    if text.startswith("/start") or text in ("/menu", "/nube"):
        await show_menu(chat_id)
        return
    if text.startswith("/ayuda") or text == "/help":
        await tg_send(chat_id, help_text(), reply_markup=help_keyboard())
        return
    if text.startswith("/estado"):
        await tg_send(chat_id, estado_text())
        return
    if text.startswith("/stats"):
        await tg_send(chat_id, stats_text())
        return
    if text.startswith("/archivos") or text == "/files":
        await tg_send(chat_id, files_list_text(),
                      reply_markup=[[{"text": "🏠  Menú",
                                      "callback_data": "menu"}]])
        return
    if text.startswith("/info"):
        p = LAST_FILE.get(chat_id)
        info = file_info(p) if p and os.path.exists(p) else None
        await tg_send(chat_id, file_detail_text(info))
        return
    if text.startswith("/borrar"):
        p = LAST_FILE.get(chat_id)
        if not p or not os.path.exists(p):
            await tg_send(chat_id, "❌ No hay archivo reciente.")
            return
        try:
            sz = os.path.getsize(p)
            os.remove(p)
            await tg_send(chat_id, f"🗑 Borrado: `{os.path.basename(p)}` "
                                   f"({human_size(sz)})")
            LAST_FILE[chat_id] = None
        except OSError as e:
            await tg_send(chat_id, f"❌ No pude borrar: `{e}`")
        return
    if text.startswith("/mover"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await tg_send(chat_id, "Uso: `/mover /tmp/destino/`")
            return
        dest_dir = parts[1].strip()
        p = LAST_FILE.get(chat_id)
        if not p or not os.path.exists(p):
            await tg_send(chat_id, "❌ No hay archivo reciente.")
            return
        try:
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
            new_path = os.path.join(dest_dir, os.path.basename(p))
            shutil.move(p, new_path)
            LAST_FILE[chat_id] = new_path
            await tg_send(chat_id, f"📦 Movido a `{new_path}`")
        except Exception as e:
            await tg_send(chat_id, f"❌ No pude mover: `{e}`")
        return
    if text.startswith("/copiar"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await tg_send(chat_id, "Uso: `/copiar /tmp/destino/`")
            return
        dest_dir = parts[1].strip()
        p = LAST_FILE.get(chat_id)
        if not p or not os.path.exists(p):
            await tg_send(chat_id, "❌ No hay archivo reciente.")
            return
        try:
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
            new_path = os.path.join(dest_dir, os.path.basename(p))
            shutil.copy2(p, new_path)
            await tg_send(chat_id, f"📋 Copiado a `{new_path}`")
        except Exception as e:
            await tg_send(chat_id, f"❌ No pude copiar: `{e}`")
        return
    if text.startswith("/cola"):
        if not ACTIVE_OPS:
            await tg_send(chat_id, "📋 *Cola vacía.* No hay operaciones activas.")
            return
        msg = "📋 *Operaciones activas:*\n\n"
        for op_id, st in ACTIVE_OPS.items():
            msg += (f"• `{op_id}` ({st.kind})\n"
                    f"  📦 `{st.name[:30]}` — {st.global_pct:.0f}%\n")
        await tg_send(chat_id, msg)
        return
    if text.startswith("/cancelar") or text == "/cancel":
        if not ACTIVE_OPS:
            await tg_send(chat_id, "❌ No hay operaciones activas para cancelar.")
            return
        for op_id in list(ACTIVE_OPS.keys()):
            await cancel_op(op_id)
        await tg_send(chat_id, f"🛑 Cancelando {len(ACTIVE_OPS)} operación(es)...")
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
        await tg_send(chat_id, file_detail_text(file_info(p)))
        return

    # URL directa?
    if text.startswith("http://") or text.startswith("https://"):
        op_id = gen_op_id()
        status = await tg_send(chat_id,
                               f"🔗 *Procesando enlace...*\n`{text[:60]}`\n"
                               f"━━━━━━━━━━━━━━━━━━━━━━━\n⏳ Iniciando...",
                               reply_markup=cancel_keyboard(op_id))
        status_msg_id = (status or {}).get("result", {}).get("message_id")
        if not status_msg_id:
            return
        state = OperationState(op_id, chat_id, "url", text)
        state.status_msg_id = status_msg_id
        ACTIVE_OPS[op_id] = state
        try:
            local = await download_url_direct(text, "/tmp", chat_id,
                                                status_msg_id, op_id)
        except asyncio.CancelledError:
            await tg_edit(chat_id, status_msg_id,
                          "❌ *Cancelado por el usuario.*", force=True)
            ACTIVE_OPS.pop(op_id, None)
            return
        except Exception as e:
            await tg_send(chat_id, f"❌ Descarga falló: `{str(e)[:200]}`")
            ACTIVE_OPS.pop(op_id, None)
            return
        if not local or not os.path.exists(local):
            await tg_send(chat_id, "❌ Descarga falló: archivo no encontrado.")
            ACTIVE_OPS.pop(op_id, None)
            return
        LAST_FILE[chat_id] = local
        size = os.path.getsize(local)
        state.total_size = size
        await tg_send(chat_id, f"✅ Descargado: `{os.path.basename(local)}` "
                               f"({human_size(size)}). Subiendo a OJS...")
        journal = CHOSEN.get(str(chat_id), JOURNAL_DEFAULT)
        try:
            lines, n_parts, sub = await upload_to_ojs(
                journal, local, os.path.basename(local), chat_id,
                status_msg_id, op_id)
        except asyncio.CancelledError:
            await tg_edit(chat_id, status_msg_id,
                          "❌ *Cancelado por el usuario.*", force=True)
            ACTIVE_OPS.pop(op_id, None)
            return
        except Exception as e:
            await tg_send(chat_id, f"❌ Error subiendo: `{str(e)[:200]}`")
            ACTIVE_OPS.pop(op_id, None)
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
        ACTIVE_OPS.pop(op_id, None)
        return

    # Texto no reconocido
    await tg_send(chat_id,
                  "🤔 No reconocí ese mensaje.\n\n"
                  "Puedes:\n"
                  "• Enviarme un *archivo* (PDF, video, etc.).\n"
                  "• Enviarme un *enlace* http(s).\n"
                  "• Usar /start para el menú.\n"
                  "• Usar /ayuda para ver todos los comandos.")


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
        f"• 📁 Gestionar archivos: `/archivos`, `/info`, `/borrar`.\n"
        f"• 📜 Enviarte `descargar_enlace.py`.\n\n"
        f"👇 Elige una nube o envíame un archivo/enlace."
    )
    if edit_msg_id:
        await tg_edit(chat_id, edit_msg_id, text,
                      reply_markup=main_menu_keyboard(), force=True)
    else:
        await tg_send(chat_id, text, reply_markup=main_menu_keyboard())


# --- Handlers de callback ---------------------------------------------------


async def handle_callback(cb):
    global PART_MB, LAST_ACTIVITY
    LAST_ACTIVITY = time.time()
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    data = cb.get("data", "")
    cb_id = cb.get("id")
    user_id = cb.get("from", {}).get("id")

    # Responder siempre al callback
    try:
        await answer_callback(cb_id)
    except Exception:
        pass

    if data == "help":
        await tg_send(chat_id, help_text(), reply_markup=help_keyboard())
        return
    if data == "estado":
        await tg_send(chat_id, estado_text())
        return
    if data == "stats":
        await tg_send(chat_id, stats_text())
        return
    if data == "archivos":
        kb = [[{"text": "📄  Info último", "callback_data": "info"},
               {"text": "🗑  Borrar último", "callback_data": "borrar"}],
              [{"text": "🏠  Menú", "callback_data": "menu"}]]
        await tg_send(chat_id, files_list_text(), reply_markup=kb)
        return
    if data == "info":
        p = LAST_FILE.get(chat_id)
        info = file_info(p) if p and os.path.exists(p) else None
        await tg_send(chat_id, file_detail_text(info))
        return
    if data == "borrar":
        p = LAST_FILE.get(chat_id)
        if not p or not os.path.exists(p):
            await tg_send(chat_id, "❌ No hay archivo reciente.")
            return
        try:
            sz = os.path.getsize(p)
            os.remove(p)
            LAST_FILE[chat_id] = None
            await tg_send(chat_id, f"🗑 Borrado: `{os.path.basename(p)}` "
                                   f"({human_size(sz)})")
        except OSError as e:
            await tg_send(chat_id, f"❌ No pude borrar: `{e}`")
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

    if data.startswith("cancel:"):
        op_id = data.split(":", 1)[1]
        ok = await cancel_op(op_id)
        if ok:
            await answer_callback(cb_id, "🛑 Cancelando...")
            await tg_edit(chat_id, msg_id,
                          f"🛑 *Cancelando operación...*\n`{op_id}`",
                          force=True)
        else:
            await answer_callback(cb_id, "❌ Operación no encontrada",
                                  show_alert=True)
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


# --- Dispatcher ------------------------------------------------------------


async def dispatch_update(up):
    """Procesa un update (message o callback_query)."""
    try:
        if "callback_query" in up:
            cb = up["callback_query"]
            log.info(">>> Recibí callback: data=%s user=%s",
                     cb.get("data"), cb.get("from", {}).get("id"))
            await handle_callback(cb)
        elif up.get("message"):
            msg = up["message"]
            chat_id = msg["chat"]["id"]
            user_id = msg.get("from", {}).get("id")
            text = (msg.get("text") or "").strip()
            log.info(">>> Recibí mensaje: chat=%s text=%r doc=%s video=%s",
                     chat_id, text[:50],
                     bool(msg.get("document")),
                     bool(msg.get("video")))
            if text:
                await handle_text(chat_id, text, user_id)
            elif (msg.get("document") or msg.get("video") or
                  msg.get("audio") or msg.get("animation") or
                  msg.get("voice") or msg.get("video_note") or
                  msg.get("photo")):
                await process_upload(msg)
        elif up.get("edited_message"):
            pass
    except Exception as e:
        log.exception(">>> dispatch error: %s", e)


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
                    await asyncio.sleep(30)
                else:
                    await asyncio.sleep(5)
                continue
            for up in data.get("result", []):
                offset = up["update_id"] + 1
                LAST_ACTIVITY = time.time()
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
                        f"active_ops={len(ACTIVE_OPS)}\n"
                        f"pending_links_chats={len(PENDING_LINKS)}\n"
                        f"logs={len(LOG_BUFFER)}\n"
                        f"pyrogram={'on' if (_pyrogram_app and _pyrogram_app.is_initialized) else 'pending'}\n"
                        ).encode()
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
                    "active_ops": len(ACTIVE_OPS),
                    "queue_size": len(OP_QUEUE),
                    "pending_links_chats": len(PENDING_LINKS),
                    "chosen": CHOSEN,
                    "pyrogram_started": bool(
                        _pyrogram_app and _pyrogram_app.is_initialized),
                    "stats": STATS,
                    "files_indexed": len(FILES_INDEX.get("files", [])),
                    "log_buffer_size": len(LOG_BUFFER),
                    "recent_logs": list(LOG_BUFFER)[-15:],
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

    log.info(">>> Bot iniciando (v2 mejorada)...")
    log.info("Config: API_ID=%s, PART_MB=%d, JOURNAL_DEFAULT=%s, IDLE_MIN=%d",
             API_ID, PART_MB, JOURNAL_DEFAULT, IDLE_MIN)
    log.info("Polling HTTP: ACTIVO | Pyrogram: lazy")

    asyncio.create_task(idle_watcher())
    asyncio.create_task(polling_loop())

    while True:
        await asyncio.sleep(2)


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("Interrumpido por usuario.")
