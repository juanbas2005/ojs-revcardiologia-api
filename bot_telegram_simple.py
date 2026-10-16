"""
Bot de Telegram (versión simple, sin Pyrogram) — robusto y fácil de usar.

Usa solo la API HTTP de bots con `requests` (polling largo). NO requiere
api_id ni api_hash.

Funcionalidades:
  - Subida a OJS por partes de PART_MB MB con renombrado a .pdf.
  - Descarga de archivos de Telegram con BARRA DE PROGRESO REAL.
    - Para archivos <=20 MB: getFile directo.
    - Para archivos >20 MB: aviso claro del límite de la API de bots.
  - Descarga de enlaces directos http(s) con barra de progreso.
  - Botones inline robustos: TODOS responden con answerCallbackQuery.
  - Comando /ayuda /help completo con todos los comandos.
  - /script envía descargar_enlace.py.
  - /config muestra y cambia revista por defecto y PART_MB.
  - /temp y /limpiar gestionan archivos temporales.
  - /renombrar, /recortar, /zip, /size sobre el último archivo.
  - /estado muestra info del bot (uptime, memoria, última actividad).
  - Persistencia de la revista elegida por chat en config.json.
  - Manejo robusto de errores: FloodWait, timeouts, errores de red.
  - Throttle de ediciones: máximo 1 edición cada 1s para no bans.
  - IDLE_MIN=0 deshabilita timeout (para Render/VPS 24/7).

Variables de entorno (GitHub Secrets o .secrets/.env local):
  TELEGRAM_BOT_TOKEN, OJS_USER, OJS_PASS, OJS_PASS_REVCARD,
  PART_MB (default 5), OJS_JOURNAL (default revcardiologia),
  IDLE_MIN (default 30).
"""
import os
import sys
import re
import json
import time
import shutil
import zipfile
import logging
import threading
from pathlib import Path
from datetime import datetime, timedelta

HERE = Path(__file__).resolve().parent
ENV_LOCAL = HERE / ".secrets" / ".env"
if not ENV_LOCAL.exists():
    ENV_LOCAL = HERE / ".env"
if ENV_LOCAL.exists():
    for line in ENV_LOCAL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

sys.path.insert(0, str(HERE))
from ojs_api import OJSApi, cargar_revista  # noqa: E402

import requests
requests.packages.urllib3.disable_warnings()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OJS_USER = os.environ.get("OJS_USER", "")
OJS_PASS = os.environ.get("OJS_PASS", "")
OJS_PASS_REVCARD = os.environ.get("OJS_PASS_REVCARD", "")
PART_MB = int(os.environ.get("PART_MB", "5"))
JOURNAL_DEFAULT = os.environ.get("OJS_JOURNAL", "revcardiologia")
IDLE_MIN = int(os.environ.get("IDLE_MIN", "30"))

# Telegram Bot API limits
TG_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024  # 20 MB límite de getFile

MAX_PART_MB = 19
if PART_MB > MAX_PART_MB:
    log.warning("PART_MB=%d > limite real (%d); ajustando", PART_MB, MAX_PART_MB)
    PART_MB = MAX_PART_MB

# revistas compatibles
with open(HERE / "revistas.json", encoding="utf-8") as f:
    REVISTAS_DICT = {r["journal"]: r for r in json.load(f)["revistas"]
                     if r.get("compatible")}
JOURNAL_SLUGS = list(REVISTAS_DICT.keys())

EXT_PERMITIDAS = {"epub", "xml", "xls", "cvs", "doc", "docx", "pdf",
                  "jpg", "png", "html", "gif", "xlsx"}

# persistence
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


LAST_FILE = {}  # chat_id -> path local
START_TIME = time.time()
LAST_ACTIVITY = time.time()

API = f"https://api.telegram.org/bot{TOKEN}"

# Throttle de ediciones: 1 mensaje como máximo cada 0.8s
_last_edit = {}  # (chat_id, msg_id) -> last_edit_ts
EDIT_THROTTLE_S = 0.8


# --- helpers HTTP a Telegram ------------------------------------------------


def tg_get(method, params=None, timeout=60):
    method = method.lstrip("/")
    return requests.get(f"{API}/{method}", params=params, timeout=timeout)


def tg_post(method, data=None, files=None, timeout=600):
    method = method.lstrip("/")
    try:
        return requests.post(f"{API}/{method}", data=data, files=files,
                             timeout=timeout)
    except requests.Timeout:
        # Reintento con timeout más largo para subidas grandes
        return requests.post(f"{API}/{method}", data=data, files=files,
                             timeout=900)


def tg_post_with_retry(method, data=None, files=None, timeout=600, retries=3):
    """POST con reintentos ante FloodWait, timeouts y errores de red."""
    method = method.lstrip("/")
    last_err = None
    for i in range(retries):
        try:
            r = requests.post(f"{API}/{method}", data=data, files=files,
                              timeout=timeout)
            j = r.json()
            if not j.get("ok"):
                # FloodWait
                if j.get("error_code") == 429:
                    wait = j.get("parameters", {}).get("retry_after", 5)
                    log.warning("FloodWait: esperando %ss", wait)
                    time.sleep(wait + 1)
                    continue
                return j
            return j
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            log.warning("POST %s falló (%s), reintento %d/%d",
                        method, e, i + 1, retries)
            time.sleep(3)
    return {"ok": False, "description": f"retries agotados: {last_err}"}


def send(chat, text, keyboard=None, parse_mode="Markdown",
         disable_web_preview=True):
    data = {"chat_id": chat, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if disable_web_preview:
        data["disable_web_page_preview"] = "true"
    if keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    return tg_post_with_retry("sendMessage", data=data)


def edit(chat, msg_id, text, keyboard=None, parse_mode="Markdown",
         force=False):
    """Edita mensaje con throttle para evitar FloodWait."""
    key = (chat, msg_id)
    now = time.time()
    if not force and key in _last_edit and now - _last_edit[key] < EDIT_THROTTLE_S:
        return None  # demasiadas ediciones, saltar
    _last_edit[key] = now
    data = {"chat_id": chat, "message_id": msg_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    try:
        return tg_post_with_retry("editMessageText", data=data, timeout=30)
    except Exception as e:
        log.debug("editMessageText: %s", e)
        return None


def send_document(chat, path, caption=""):
    """Envía documento con retry y soporte de archivos grandes."""
    try:
        with open(path, "rb") as f:
            return tg_post_with_retry("sendDocument",
                                      data={"chat_id": chat, "caption": caption,
                                            "parse_mode": "Markdown"},
                                      files={"document": (os.path.basename(path), f)},
                                      timeout=900)
    except Exception as e:
        log.error("sendDocument: %s", e)
        return None


def answer_callback(cb_id, text=None, show_alert=False):
    """Responde a un callback_query para que el botón deje de 'cargar'."""
    if not cb_id:
        return
    data = {"callback_query_id": cb_id}
    if text:
        data["text"] = text[:200]
    if show_alert:
        data["show_alert"] = True
    tg_post_with_retry("answerCallbackQuery", data=data, timeout=10)


def progress_bar(done, total, width=12):
    """Barra de progreso visual con caracteres Unicode."""
    if total <= 0:
        return "░" * width + " ?"
    frac = min(done / total, 1.0)
    filled = int(frac * width)
    return "█" * filled + "░" * (width - filled)


def human_size(n):
    """Convierte bytes a formato legible."""
    if n is None or n < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def human_speed(bytes_per_sec):
    if bytes_per_sec <= 0:
        return "? B/s"
    return f"{human_size(bytes_per_sec)}/s"


def safe_filename(name):
    base, ext = os.path.splitext(name)
    ext = ext.lstrip(".").lower()
    if ext not in EXT_PERMITIDAS:
        ext = "pdf"
    return base + "." + ext, ext


def sanitize_markdown(text):
    """Escapa caracteres especiales de Markdown para evitar errores."""
    if not text:
        return ""
    # solo escapar lo que Markdown V1 interpreta
    return re.sub(r"([_*`\[])", r"\\\1", text[:3000])


# --- menús -----------------------------------------------------------------


def menu_keyboard():
    """Teclado inline principal con las revistas."""
    kb = [[{"text": f"☁️  {s}", "callback_data": f"nube:{s}"}]
          for s in JOURNAL_SLUGS]
    kb.append([{"text": "🛠  Ajustes", "callback_data": "config"},
               {"text": "❓  Ayuda", "callback_data": "help"},
               {"text": "📊  Estado", "callback_data": "estado"}])
    kb.append([{"text": "📜  Script descarga", "callback_data": "script"},
               {"text": "🧹  Limpiar temporales", "callback_data": "limpiar"}])
    return kb


def menu(chat, edit_msg_id=None):
    actual = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
    text = (
        f"👋 *Bot OJS Cubanas*\n\n"
        f"☁️ *Nube actual:* `{actual}`\n"
        f"📦 *Tamaño de parte:* `{PART_MB} MB` (máx 19 MB)\n\n"
        f"*¿Qué puedo hacer?*\n"
        f"• 📤 Subir archivos a revistas OJS por partes.\n"
        f"• ⬇️ Descargar enlaces directos http(s).\n"
        f"• 🎬 Renombrar, recortar, comprimir archivos.\n"
        f"• 📜 Enviarte `descargar_enlace.py`.\n\n"
        f"👇 Elige una nube o envíame un archivo/enlace."
    )
    if edit_msg_id:
        edit(chat, edit_msg_id, text, keyboard=menu_keyboard(), force=True)
    else:
        send(chat, text, keyboard=menu_keyboard())


def help_text():
    return (
        "❓ *Ayuda del Bot OJS*\n\n"
        "*Comandos disponibles:*\n\n"
        "📤 *Subir archivos a OJS:*\n"
        "• Envía un archivo/documento/video.\n"
        "• Lo divido en partes de `5 MB` (máx 19 MB).\n"
        "• Renombro a `.pdf` si la extensión no está permitida.\n"
        "• Te devuelvo un `.txt` con todos los enlaces.\n\n"
        "⬇️ *Descargar de Internet:*\n"
        "• Envía un enlace `http(s)` y lo descargo + subo.\n\n"
        "⚙️ *Comandos:*\n"
        "• `/start` `/menu` — Menú principal.\n"
        "• `/ayuda` `/help` — Esta ayuda.\n"
        "• `/config` — Ver y cambiar ajustes.\n"
        "• `/estado` — Estado del bot (uptime, memoria).\n"
        "• `/script` — Recibir `descargar_enlace.py`.\n"
        "• `/temp` — Listar archivos temporales.\n"
        "• `/limpiar` — Borrar archivos temporales.\n"
        "• `/renombrar <nombre>` — Renombrar último archivo.\n"
        "• `/recortar <bytes>` — Dejar solo los primeros N bytes.\n"
        "• `/zip` — Comprimir último archivo en `.zip`.\n"
        "• `/size` `/tamaño` — Tamaño del último archivo.\n\n"
        "⚠️ *Límites:*\n"
        "• Telegram Bot API: descarga directa máx `20 MB` por archivo.\n"
        "  Para archivos mayores, usa Pyrogram (ver `GUIA.md`).\n"
        "• OJS: máximo `19 MB` por archivo subido.\n"
        "• Plan free de Render: el servicio se duerme tras 15 min\n"
        "  sin actividad; usa UptimeRobot para mantenerlo vivo.\n\n"
        "💡 *Tip:* si falla una subida, prueba con `/limpiar` y reintenta."
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
        f"📊 *Estado del Bot*\n\n"
        f"• ⏱ *Uptime:* {timedelta(seconds=uptime)}\n"
        f"• 🕐 *Última actividad:* {idle}s atrás\n"
        f"• 💾 *Memoria:* {mem_mb:.0f} MB\n"
        f"• ☁️ *Nube por defecto:* `{JOURNAL_DEFAULT}`\n"
        f"• 📦 *PART_MB:* `{PART_MB}`\n"
        f"• 🔢 *Revistas compatibles:* {len(JOURNAL_SLUGS)}\n"
        f"• 🌐 *Modo:* {'24/7 (IDLE_MIN=0)' if IDLE_MIN == 0 else f'idle {IDLE_MIN}min'}"
    )


# --- descarga de Telegram con barra de progreso ----------------------------


def download_from_telegram(file_id, dest_path, nombre, chat, status_msg_id):
    """Descarga un archivo de Telegram con barra de progreso real.
    Devuelve la ruta local o lanza RuntimeError.
    Telegram Bot API limita la descarga a 20 MB vía getFile."""
    # 1. getFile
    try:
        j = tg_get("/getFile", params={"file_id": file_id}, timeout=60).json()
    except Exception as e:
        raise RuntimeError(f"getFile falló: {e}")
    if not j.get("ok"):
        # 400 file too big = archivo > 20 MB
        if j.get("error_code") == 400 and "too big" in j.get("description", "").lower():
            raise RuntimeError(
                f"El archivo supera los 20 MB (límite de la API de bots de "
                f"Telegram). No puedo descargarlo con este método. Para "
                f"archivos grandes, pídele al administrador que configure "
                f"API_ID/API_HASH y use el bot con Pyrogram.")
        raise RuntimeError(f"getFile error: {j}")

    file_info = j["result"]
    file_size = file_info.get("file_size", 0)
    file_path = file_info["file_path"]

    # 2. Descargar con streaming y barra de progreso
    download_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
    try:
        r = requests.get(download_url, stream=True, timeout=120,
                         verify=True)
    except Exception as e:
        raise RuntimeError(f"conexión a Telegram falló: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} descargando de Telegram")

    total = int(r.headers.get("Content-Length", 0)) or file_size
    written = 0
    last_edit = 0
    t0 = time.time()
    try:
        with open(dest_path, "wb") as out:
            for chunk in r.iter_content(128 * 1024):
                if not chunk:
                    continue
                out.write(chunk)
                written += len(chunk)
                now = time.time()
                if now - last_edit >= 1.0:
                    last_edit = now
                    elapsed = now - t0
                    speed = written / elapsed if elapsed > 0 else 0
                    pct = (written / total * 100) if total else 0
                    bar = progress_bar(written, total)
                    eta = ((total - written) / speed) if speed > 0 else 0
                    edit(chat, status_msg_id,
                         f"⬇️ Descargando de Telegram\n"
                         f"`{nombre[:40]}`\n\n"
                         f"{bar} {pct:.0f}%\n"
                         f"{human_size(written)} / {human_size(total)}\n"
                         f"⚡ {human_speed(speed)} | ETA {int(eta)}s")
    except Exception as e:
        # borrar parcial
        try:
            os.remove(dest_path)
        except OSError:
            pass
        raise RuntimeError(f"descarga interrumpida: {e}")
    return dest_path


# --- upload a OJS por partes con barra de progreso -------------------------


def upload_to_ojs(journal, filepath, original_name, chat, status_msg_id):
    """Sube `filepath` a `journal` por partes. Devuelve (lines, n_parts, sub)."""
    host, _ = cargar_revista(journal)
    pw = OJS_PASS_REVCARD if journal == "revcardiologia" else OJS_PASS
    api = OJSApi(host, OJS_USER, pw, journal=journal)
    size = os.path.getsize(filepath)

    # 1. Login
    edit(chat, status_msg_id, f"🔐 Logueando en *{journal}*...", force=True)
    try:
        api.login()
    except Exception as e:
        raise RuntimeError(f"login {journal}: {e}")

    # 2. Crear submission
    edit(chat, status_msg_id,
         f"📝 Creando submission en *{journal}*...", force=True)
    try:
        sub = int(api.create())
    except Exception as e:
        raise RuntimeError(f"crear submission {journal}: {e}")

    base, ext = safe_filename(original_name)
    total_parts = max(1, (size + PART_MB * 1024 * 1024 - 1) //
                      (PART_MB * 1024 * 1024))

    lines = [f"# «{original_name}» ({size} bytes), {PART_MB} MB por parte",
             f"# Submission {sub} en {journal}",
             f"# Fecha: {datetime.now().isoformat(timespec='seconds')}"]
    idx = 0
    t_start = time.time()
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

            def cb(s, total, _idx=idx, _tot=total_parts,
                   _sz=part_size, _t0=time.time()):
                now = time.time()
                elapsed = now - _t0
                speed = s / elapsed if elapsed > 0 else 0
                pct = (s / total * 100) if total else 0
                bar = progress_bar(s, total)
                eta = ((total - s) / speed) if speed > 0 else 0
                # Progreso global: partes completadas + progreso de la actual
                global_pct = ((_idx - 1) + pct / 100) / _tot * 100
                edit(chat, status_msg_id,
                     f"☁️ Subiendo a *{journal}* (parte {_idx}/{_tot})\n\n"
                     f"{bar} {pct:.0f}% de esta parte\n"
                     f"{human_size(s)} / {human_size(total)}\n"
                     f"⚡ {human_speed(speed)} | ETA {int(eta)}s\n\n"
                     f"📊 Global: {global_pct:.0f}% ({_idx-1}/{_tot} partes)")

            try:
                res = api.upload(sub, part_path,
                                 filename=os.path.basename(part_path),
                                 progress_cb=cb)
                lines.append(api.download_url(sub, res["id"]))
            except Exception as e:
                err = str(e)[:200]
                lines.append(f"# ERROR en parte {idx}: {err}")
                # avisar al usuario
                edit(chat, status_msg_id,
                     f"⚠️ Error en parte {idx}/{total_parts}: `{err}`\n"
                     f"Continuando con la siguiente...", force=True)
            try:
                os.remove(part_path)
            except OSError:
                pass
    elapsed = time.time() - t_start
    edit(chat, status_msg_id,
         f"✅ Subida completada en {journal}\n"
         f"{idx} partes en {int(elapsed)}s", force=True)
    return lines, idx, sub


# --- descarga URL directa con barra de progreso ----------------------------


def download_url_direct(url, dest_dir, chat, status_msg_id):
    """Descarga URL directa con barra de progreso. Devuelve ruta local."""
    fname = url.rstrip("/").split("/")[-1] or "archivo.bin"
    fname = fname.split("?")[0]
    # nombre seguro
    fname = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", fname)[:100]
    if not fname:
        fname = "archivo.bin"
    dest = os.path.join(dest_dir, fname)

    try:
        r = requests.get(url, stream=True, timeout=60, verify=False,
                         headers={"User-Agent": "Mozilla/5.0 (X11; Linux)"})
    except Exception as e:
        raise RuntimeError(f"conexión falló: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")

    total = int(r.headers.get("Content-Length", 0))
    written = 0
    last_edit = 0
    t0 = time.time()
    try:
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
                    edit(chat, status_msg_id,
                         f"⬇️ Descargando de Internet\n`{fname[:40]}`\n\n"
                         f"{bar} {pct:.0f}%\n"
                         f"{human_size(written)} / {human_size(total)}\n"
                         f"⚡ {human_speed(speed)} | ETA {int(eta)}s")
    except Exception as e:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise RuntimeError(f"descarga interrumpida: {e}")
    return dest


# --- procesar archivo recibido --------------------------------------------


def procesar_archivo(chat, file_id, nombre, size):
    """Procesa un archivo recibido en el chat: lo descarga y sube a OJS."""
    journal = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
    if journal not in REVISTAS_DICT:
        send(chat, f"❌ Revista no válida: `{journal}`. Usa /start para elegir.")
        return

    # Avisar tamaño y límite
    size_human = human_size(size)
    size_warn = ""
    if size > TG_DOWNLOAD_MAX_BYTES:
        size_warn = (
            f"\n\n⚠️ *Atención:* este archivo supera el límite de "
            f"`20 MB` de la API de bots de Telegram. Probablemente no pueda "
            f"descargarlo. Si falla, considera enviarlo en partes más "
            f"pequeñas o usar el bot con Pyrogram.")

    status = send(chat, f"📥 Recibí `{nombre[:50]}` ({size_human}). "
                        f"Nube: *{journal}*{size_warn}")
    status_msg_id = (status or {}).get("result", {}).get("message_id")
    if not status_msg_id:
        # reintentar sin markdown
        status = send(chat, f"Recibi {nombre[:50]} ({size_human}). "
                            f"Nube: {journal}{size_warn}", parse_mode=None)
        status_msg_id = (status or {}).get("result", {}).get("message_id")

    tmp = "/tmp"
    safe_name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", nombre)[:100]
    local = os.path.join(tmp, f"tg_{int(time.time())}_{safe_name}")

    # 1. Descargar de Telegram
    try:
        download_from_telegram(file_id, local, nombre, chat, status_msg_id)
    except RuntimeError as e:
        send(chat, f"❌ {e}")
        try:
            os.remove(local)
        except OSError:
            pass
        return
    except Exception as e:
        send(chat, f"❌ Error inesperado descargando: {e}")
        try:
            os.remove(local)
        except OSError:
            pass
        return

    actual_size = os.path.getsize(local)
    LAST_FILE[chat] = local

    # 2. Subir a OJS
    try:
        lines, n_parts, sub = upload_to_ojs(journal, local, nombre, chat,
                                            status_msg_id)
    except Exception as e:
        send(chat, f"❌ Error subiendo a *{journal}*:\n`{str(e)[:300]}`")
        return

    # 3. Generar .txt con enlaces
    txt = os.path.join(tmp, f"enlaces_{Path(safe_name).stem}.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    enlaces = [l for l in lines if not l.startswith("#")]
    errores = [l for l in lines if l.startswith("# ERROR")]

    # Guardar enlaces para el botón "Ver enlaces"
    if enlaces:
        PENDING_LINKS[str(chat)] = enlaces

    # 4. Botones
    kb = [
        [{"text": "📋  Ver enlaces", "callback_data": "ver_enlaces"},
         {"text": "📜  Script descarga", "callback_data": "script"}],
        [{"text": "☁️  Cambiar nube", "callback_data": "menu"},
         {"text": "🛠  Ajustes", "callback_data": "config"}],
    ]

    caption = (f"✅ *{n_parts} partes* subidas a *{journal}*\n"
               f"📦 Submission: `{sub}`\n"
               f"📊 Tamaño: `{human_size(actual_size)}`")
    if errores:
        caption += f"\n⚠️ {len(errores)} parte(s) con error (ver .txt)"

    send_document(chat, txt, caption=caption)

    # 5. Enlaces inline (si son pocos)
    if len(enlaces) <= 4:
        enlace_text = "\n".join(f"`{e}`" for e in enlaces)
        send(chat, f"📋 *Enlaces generados:*\n\n{enlace_text}", keyboard=kb)
    else:
        send(chat, f"📋 *{len(enlaces)} enlaces* en el .txt adjunto arriba. "
                   f"Toca *Ver enlaces* para mostrarlos aquí.",
             keyboard=kb)


# --- dispatcher ------------------------------------------------------------


def handle_text(chat, text):
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.time()
    text = (text or "").strip()

    if text.startswith("/start") or text in ("/menu", "/nube"):
        menu(chat)
        return
    if text.startswith("/ayuda") or text == "/help":
        send(chat, help_text())
        return
    if text.startswith("/estado"):
        send(chat, estado_text())
        return
    if text.startswith("/script"):
        script_path = HERE / "descargar_enlace.py"
        if script_path.exists():
            send_document(chat, str(script_path),
                         caption="📜 *descargar_enlace.py*\n\n"
                                 "Ejecútalo en tu PC para bajar los "
                                 "enlaces que produce el bot.")
        else:
            send(chat, "❌ No encuentro `descargar_enlace.py` en el servidor.")
        return
    if text.startswith("/temp"):
        files = sorted(Path("/tmp").glob("tg_*"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:20]
        if not files:
            send(chat, "✅ No hay archivos temporales en `/tmp`.")
            return
        msg = "🧹 *Temporales* (últimos 20):\n\n"
        total = 0
        for p in files:
            sz = p.stat().st_size
            total += sz
            msg += f"• `{p.name}` ({human_size(sz)})\n"
        msg += f"\n📊 *Total:* {human_size(total)}\n\n"
        msg += "Usa `/limpiar` para borrarlos."
        send(chat, msg)
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
        send(chat, f"🧹 Limpié *{n}* archivos ({human_size(freed)} liberados).")
        return
    if text.startswith("/config"):
        kb = [[{"text": f"📡  {s}", "callback_data": f"setdef:{s}"}]
              for s in JOURNAL_SLUGS]
        kb.append([{"text": "➕ PART_MB +1", "callback_data": "partmb:1"},
                   {"text": "➖ PART_MB -1", "callback_data": "partmb:-1"},
                   {"text": "↩️ 5 MB", "callback_data": "partmb:5"}])
        actual = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
        send(chat,
             f"⚙️ *Ajustes*\n\n"
             f"• Revista por defecto: `{actual}`\n"
             f"• PART_MB: `{PART_MB}` MB (límite OJS: 19 MB)\n"
             f"• Idle: `{IDLE_MIN}` min\n"
             f"• Bot: `@Uptimeriroobot`\n\n"
             f"👇 Toca para cambiar:",
             keyboard=kb)
        return
    if text.startswith("/renombrar"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send(chat, "Uso: `/renombrar nuevo_nombre.pdf`")
            return
        new_name = parts[1].strip()
        p = LAST_FILE.get(chat)
        if not p or not os.path.exists(p):
            send(chat, "❌ No hay archivo reciente. Envíame uno primero.")
            return
        new_path = os.path.join(os.path.dirname(p), new_name)
        try:
            os.rename(p, new_path)
            LAST_FILE[chat] = new_path
            send_document(chat, new_path,
                          caption=f"✅ Renombrado a `{new_name}`")
        except OSError as e:
            send(chat, f"❌ No pude renombrar: `{e}`")
        return
    if text.startswith("/recortar"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            send(chat, "Uso: `/recortar 1000000` (deja los primeros N bytes)")
            return
        n = int(parts[1].strip())
        p = LAST_FILE.get(chat)
        if not p or not os.path.exists(p):
            send(chat, "❌ No hay archivo reciente. Envíame uno primero.")
            return
        new_path = p + ".recortado"
        try:
            with open(p, "rb") as f, open(new_path, "wb") as g:
                g.write(f.read(n))
            LAST_FILE[chat] = new_path
            send_document(chat, new_path,
                          caption=f"✅ Recortado a `{human_size(n)}`")
        except OSError as e:
            send(chat, f"❌ No pude recortar: `{e}`")
        return
    if text.startswith("/zip"):
        p = LAST_FILE.get(chat)
        if not p or not os.path.exists(p):
            send(chat, "❌ No hay archivo reciente. Envíame uno primero.")
            return
        zip_path = p + ".zip"
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(p, os.path.basename(p))
            LAST_FILE[chat] = zip_path
            send_document(chat, zip_path,
                          caption=f"✅ Comprimido en `.zip` "
                                  f"({human_size(os.path.getsize(zip_path))})")
        except Exception as e:
            send(chat, f"❌ No pude comprimir: `{e}`")
        return
    if text.startswith("/tamano") or text.startswith("/tamaño") or \
            text.startswith("/size"):
        p = LAST_FILE.get(chat)
        if not p or not os.path.exists(p):
            send(chat, "❌ No hay archivo reciente. Envíame uno primero.")
            return
        send(chat, f"📄 `{os.path.basename(p)}`\n"
                   f"📊 Tamaño: `{human_size(os.path.getsize(p))}`")
        return

    # URL directa?
    if text.startswith("http://") or text.startswith("https://"):
        status = send(chat, "🔗 Procesando enlace...")
        status_msg_id = (status or {}).get("result", {}).get("message_id")
        if not status_msg_id:
            send(chat, "❌ No pude crear mensaje de estado.")
            return
        try:
            local = download_url_direct(text, "/tmp", chat, status_msg_id)
        except Exception as e:
            send(chat, f"❌ Descarga falló: `{str(e)[:200]}`")
            return
        LAST_FILE[chat] = local
        size = os.path.getsize(local)
        send(chat, f"✅ Descargado: `{os.path.basename(local)}` "
                   f"({human_size(size)}). Subiendo a OJS...")
        journal = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
        try:
            lines, n_parts, sub = upload_to_ojs(
                journal, local, os.path.basename(local), chat, status_msg_id)
        except Exception as e:
            send(chat, f"❌ Error subiendo: `{str(e)[:200]}`")
            return
        txt = f"/tmp/enlaces_{Path(local).stem}.txt"
        with open(txt, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        send_document(chat, txt,
                      caption=f"✅ {n_parts} partes en *{journal}* (sub `{sub}`)")
        return

    # Texto que no es comando ni URL
    send(chat,
         "🤔 No reconocí ese mensaje.\n\n"
         "Puedes:\n"
         "• Enviarme un *archivo* (PDF, video, etc.).\n"
         "• Enviarme un *enlace* http(s).\n"
         "• Usar /start para el menú.\n"
         "• Usar /ayuda para ver todos los comandos.")


def handle_message(msg):
    chat = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if text:
        handle_text(chat, text)
        return
    doc = msg.get("document")
    if doc:
        procesar_archivo(chat, doc["file_id"],
                        doc.get("file_name", "archivo"),
                        doc.get("file_size", 0))
        return
    vid = msg.get("video") or msg.get("video_note") or msg.get("audio") or \
        msg.get("voice") or msg.get("animation")
    if vid:
        procesar_archivo(chat, vid["file_id"],
                        vid.get("file_name") or f"media_{int(time.time())}.mp4",
                        vid.get("file_size", 0))
        return
    # foto (Telegram comprime, file_size puede no venir)
    photo = msg.get("photo")
    if photo:
        # la última es la de mayor resolución
        biggest = photo[-1]
        procesar_archivo(chat, biggest["file_id"],
                         f"foto_{int(time.time())}.jpg",
                         biggest.get("file_size", 0))
        return
    send(chat, "🤔 Recibí un mensaje que no puedo procesar. Usa /ayuda.")


# Estado por chat para mostrar enlaces pendientes
PENDING_LINKS = {}  # chat_id -> lista de enlaces


def handle_callback(cb):
    global PART_MB
    chat = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    data = cb.get("data", "")
    cb_id = cb.get("id")

    # responder siempre al callback para que deje de "cargar"
    try:
        answer_callback(cb_id)
    except Exception:
        pass

    if data == "help":
        send(chat, help_text())
        return
    if data == "estado":
        send(chat, estado_text())
        return
    if data == "menu":
        menu(chat, edit_msg_id=msg_id)
        return
    if data == "config":
        kb = [[{"text": f"📡  {s}", "callback_data": f"setdef:{s}"}]
              for s in JOURNAL_SLUGS]
        kb.append([{"text": "➕ PART_MB +1", "callback_data": "partmb:1"},
                   {"text": "➖ PART_MB -1", "callback_data": "partmb:-1"},
                   {"text": "↩️ 5 MB", "callback_data": "partmb:5"}])
        actual = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
        edit(chat, msg_id,
             f"⚙️ *Ajustes*\n\n"
             f"• Revista: `{actual}`\n"
             f"• PART_MB: `{PART_MB}` MB\n"
             f"• Idle: `{IDLE_MIN}` min\n\n"
             f"👇 Toca para cambiar:",
             keyboard=kb, force=True)
        return
    if data == "script":
        script_path = HERE / "descargar_enlace.py"
        if script_path.exists():
            send_document(chat, str(script_path),
                         caption="📜 *descargar_enlace.py* para bajar "
                                 "los enlaces en tu PC.")
        else:
            send(chat, "❌ No encuentro `descargar_enlace.py`.")
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
        edit(chat, msg_id,
             f"🧹 Limpié *{n}* archivos ({human_size(freed)} liberados).",
             force=True)
        return
    if data == "temp":
        handle_text(chat, "/temp")
        return
    if data == "ver_enlaces":
        enlaces = PENDING_LINKS.get(str(chat), [])
        if not enlaces:
            send(chat, "❌ No hay enlaces recientes para mostrar. "
                       "Sube un archivo primero.")
            return
        if len(enlaces) <= 4:
            text = "\n".join(f"`{e}`" for e in enlaces)
        else:
            text = "\n".join(f"`{e}`" for e in enlaces[:8])
            text += f"\n\n... y {len(enlaces)-8} más."
        kb = [[{"text": "📋  Copiar", "callback_data": "copiar_enlaces"}]]
        send(chat, f"📋 *Enlaces:*\n\n{text}", keyboard=kb)
        return
    if data == "copiar_enlaces":
        enlaces = PENDING_LINKS.get(str(chat), [])
        if enlaces:
            # Telegram no permite copiar al portapapeles via bot, pero
            # enviar como code block permite copiar fácilmente
            text = "\n".join(enlaces)
            # dividir en chunks de 4000 chars
            for i in range(0, len(text), 4000):
                send(chat, f"```\n{text[i:i+4000]}\n```")
        return

    if data.startswith("nube:"):
        slug = data.split(":", 1)[1]
        if slug not in REVISTAS_DICT:
            answer_callback(cb_id, "Revista no válida", show_alert=True)
            return
        persist_chosen(chat, slug)
        answer_callback(cb_id, f"✅ Nube: {slug}")
        edit(chat, msg_id,
             f"✅ *Nube elegida:* `{slug}`\n\n"
             f"Ahora envíame el archivo que quieres subir.",
             force=True)
        return

    if data.startswith("setdef:"):
        slug = data.split(":", 1)[1]
        if slug not in REVISTAS_DICT:
            answer_callback(cb_id, "Revista no válida", show_alert=True)
            return
        persist_chosen(chat, slug)
        answer_callback(cb_id, f"✅ Por defecto: {slug}")
        edit(chat, msg_id,
             f"✅ *Revista por defecto:* `{slug}`",
             force=True)
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
        answer_callback(cb_id, f"✅ PART_MB = {PART_MB} MB")
        edit(chat, msg_id,
             f"✅ *PART_MB = `{PART_MB}` MB*\n"
             f"(antes: {old} MB, límite OJS: {MAX_PART_MB} MB)",
             force=True)
        return

    # callback desconocido
    answer_callback(cb_id, "Acción no reconocida", show_alert=True)


def dispatch(up):
    if "callback_query" in up:
        handle_callback(up["callback_query"])
    elif up.get("message") or up.get("edited_message"):
        handle_message(up["message"] or up["edited_message"])


# --- main loop -------------------------------------------------------------


def main():
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.time()
    # health check inicial
    try:
        me = tg_get("/getMe", timeout=15).json()
    except Exception as e:
        sys.exit(f"No pude conectar a Telegram: {e}")
    if not me.get("ok"):
        sys.exit(f"Bot no responde a getMe: {me}")
    log.info("Bot iniciado como @%s (id=%s)",
             me["result"]["username"], me["result"]["id"])
    log.info("Config: PART_MB=%d, JOURNAL_DEFAULT=%s, IDLE_MIN=%d",
             PART_MB, JOURNAL_DEFAULT, IDLE_MIN)

    offset = 0
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"timeout": 25, "offset": offset,
                                     "allowed_updates": json.dumps(
                                         ["message", "edited_message",
                                          "callback_query"])},
                             timeout=60).json()
        except requests.Timeout:
            continue
        except Exception as e:
            log.warning("getUpdates: %s; reintentando en 5s", e)
            time.sleep(5)
            continue
        if not r.get("ok"):
            log.warning("getUpdates error: %s", r)
            time.sleep(5)
            continue
        for up in r.get("result", []):
            offset = up["update_id"] + 1
            try:
                dispatch(up)
            except Exception as e:
                log.exception("dispatch error: %s", e)
            LAST_ACTIVITY = time.time()
        # IDLE_MIN=0 deshabilita el timeout
        if IDLE_MIN > 0 and time.time() - LAST_ACTIVITY > IDLE_MIN * 60:
            log.info("Sin actividad por %d min, cerrando.", IDLE_MIN)
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Interrumpido por usuario.")
        sys.exit(0)
