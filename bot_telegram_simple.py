"""
Bot de Telegram (version simple, sin Pyrogram) — respaldo funcional cuando
no se disponen de API_ID/API_HASH para Pyrogram.

Usa solo la API HTTP de bots con `requests` (polling largo). NO requiere
api_id ni api_hash. Mantiene las funciones principales:

  - Subida a OJS por partes de PART_MB MB con renombrado a .pdf.
  - Botones inline (inline_keyboard) para elegir nube.
  - Comando /script para enviar descargar_enlace.py al chat.
  - /config muestra y cambia revista por defecto.
  - /temp y /limpiar gestionan archivos temporales.
  - /renombrar, /recortar, /zip, /tamaño sobre el ultimo archivo procesado.
  - Descarga de enlaces directos http(s) (sin yt-dlp).
  - Barra de progreso (aprox) durante subida a OJS y descarga de URL.
  - Persistencia de la revista elegida por chat en config.json.
  - Idle timeout: si pasa IDLE_MIN minutos sin actividad, el job termina
    (para que GitHub Actions pueda re-lanzarlo via cron).

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
log = logging.getLogger("bot_simple")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OJS_USER = os.environ.get("OJS_USER", "")
OJS_PASS = os.environ.get("OJS_PASS", "")
OJS_PASS_REVCARD = os.environ.get("OJS_PASS_REVCARD", "")
PART_MB = int(os.environ.get("PART_MB", "5"))
JOURNAL_DEFAULT = os.environ.get("OJS_JOURNAL", "revcardiologia")
IDLE_MIN = int(os.environ.get("IDLE_MIN", "30"))

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

API = f"https://api.telegram.org/bot{TOKEN}"

# --- helpers ---------------------------------------------------------------


def tg_get(method, params=None, timeout=60):
    # Aceptar method con o sin '/' inicial (normalizar a uno solo)
    method = method.lstrip("/")
    return requests.get(f"{API}/{method}", params=params, timeout=timeout)


def tg_post(method, data=None, files=None, timeout=600):
    method = method.lstrip("/")
    return requests.post(f"{API}/{method}", data=data, files=files,
                         timeout=timeout)


def send(chat, text, keyboard=None, parse_mode="Markdown"):
    data = {"chat_id": chat, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    try:
        return tg_post("sendMessage", data=data).json()
    except Exception as e:
        log.error("sendMessage: %s", e)


def edit(chat, msg_id, text, keyboard=None, parse_mode="Markdown"):
    data = {"chat_id": chat, "message_id": msg_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    try:
        return tg_post("editMessageText", data=data)
    except Exception as e:
        log.debug("editMessageText: %s", e)


def send_document(chat, path, caption=""):
    with open(path, "rb") as f:
        return tg_post("sendDocument",
                       data={"chat_id": chat, "caption": caption,
                             "parse_mode": "Markdown"},
                       files={"document": (os.path.basename(path), f)})


def progress_bar(done, total, width=10):
    if total <= 0:
        return "░" * width
    frac = min(done / total, 1.0)
    filled = int(frac * width)
    return "█" * filled + "░" * (width - filled)


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def safe_filename(name):
    base, ext = os.path.splitext(name)
    ext = ext.lstrip(".").lower()
    if ext not in EXT_PERMITIDAS:
        ext = "pdf"
    return base + "." + ext, ext


# --- menu ------------------------------------------------------------------


def menu(chat):
    kb = [[{"text": f"☁️ {s}", "callback_data": f"nube:{s}"}]
          for s in JOURNAL_SLUGS]
    kb.append([{"text": "🛠 Ajustes", "callback_data": "config"},
               {"text": "ℹ️ Ayuda", "callback_data": "help"}])
    kb.append([{"text": "📜 Ver script de descarga", "callback_data": "script"},
               {"text": "🧹 Limpiar temporales", "callback_data": "temp"}])
    actual = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
    send(chat,
         f"👋 *Bot OJS Cubanas* (versión simple, sin Pyrogram)\n\n"
         f"Nube actual: `{actual}`\n\n"
         f"*Qué puedo hacer:*\n"
         f"• Subir archivos a OJS por partes de {PART_MB} MB.\n"
         f"• Descargar enlaces directos http(s).\n"
         f"• Renombrar, recortar, comprimir (zip).\n"
         f"• Enviarte `descargar_enlace.py`.\n\n"
         f"Envíame un archivo o un enlace para empezar.",
         keyboard=kb)


# --- upload a OJS por partes -----------------------------------------------


def upload_to_ojs(journal, filepath, original_name, chat, status_msg_id):
    """Sube `filepath` a `journal` por partes. Devuelve (lines, n_parts, sub).
    `status_msg_id` es el mensaje a editar con progreso."""
    host, _ = cargar_revista(journal)
    pw = OJS_PASS_REVCARD if journal == "revcardiologia" else OJS_PASS
    api = OJSApi(host, OJS_USER, pw, journal=journal)
    try:
        edit(chat, status_msg_id, f"🔐 Login en {journal}...")
        api.login()
    except Exception as e:
        raise RuntimeError(f"login {journal}: {e}")
    edit(chat, status_msg_id, f"📝 Creando submission en {journal}...")
    sub = int(api.create())
    base, ext = safe_filename(original_name)
    size = os.path.getsize(filepath)
    total_parts = max(1, (size + PART_MB * 1024 * 1024 - 1) //
                      (PART_MB * 1024 * 1024))

    lines = [f"# «{original_name}» ({size} bytes), {PART_MB} MB por parte",
             f"# Submission {sub} en {journal}"]
    idx = 0
    with open(filepath, "rb") as f:
        while True:
            data = f.read(PART_MB * 1024 * 1024)
            if not data:
                break
            idx += 1
            part_path = f"/tmp/{base}_parte{idx:03d}.{ext}"
            with open(part_path, "wb") as g:
                g.write(data)
            sent = [0]
            part_size = len(data)

            def cb(s, total, _idx=idx, _tot=total_parts, _sent=sent,
                   _sz=part_size):
                pct = (s / total * 100) if total else 0
                bar = progress_bar(s, total)
                edit(chat, status_msg_id,
                     f"☁️ Subiendo parte {_idx}/{_tot} a {journal}\n"
                     f"{bar} {pct:.0f}% "
                     f"({human_size(s)}/{human_size(total)})")

            try:
                res = api.upload(sub, part_path,
                                 filename=os.path.basename(part_path),
                                 progress_cb=cb)
                lines.append(api.download_url(sub, res["id"]))
            except Exception as e:
                lines.append(f"# ERROR en parte {idx}: {e}")
            try:
                os.remove(part_path)
            except OSError:
                pass
    return lines, idx, sub


# --- descarga URL directa -------------------------------------------------


def download_url_direct(url, dest_dir, chat, status_msg_id):
    fname = url.rstrip("/").split("/")[-1] or "archivo.bin"
    fname = fname.split("?")[0]
    dest = os.path.join(dest_dir, fname)
    r = requests.get(url, stream=True, timeout=60, verify=False,
                     headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    total = int(r.headers.get("Content-Length", 0))
    written = 0
    last = 0
    with open(dest, "wb") as f:
        for chunk in r.iter_content(64 * 1024):
            if not chunk:
                continue
            f.write(chunk)
            written += len(chunk)
            now = time.time()
            if now - last > 1.5:
                last = now
                pct = (written / total * 100) if total else 0
                bar = progress_bar(written, total)
                edit(chat, status_msg_id,
                     f"⬇️ {fname}\n{bar} {pct:.0f}% "
                     f"({human_size(written)}/{human_size(total)})")
    return dest


# --- procesar archivo recibido --------------------------------------------


def procesar_archivo(chat, file_id, nombre, size):
    journal = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
    if journal not in REVISTAS_DICT:
        send(chat, f"❌ Revista no válida: {journal}. Usa /start")
        return
    status = send(chat, f"📥 Recibí `{nombre}` ({human_size(size)}). "
                        f"Nube: *{journal}*")
    status_msg_id = status.get("result", {}).get("message_id")
    tmp = "/tmp"
    local = os.path.join(tmp, f"tg_{int(time.time())}_{nombre}")
    # descargar de Telegram
    try:
        edit(chat, status_msg_id, "⬇️ Descargando de Telegram...")
        j = tg_get("/getFile", params={"file_id": file_id}).json()["result"]
        r = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/"
                         f"{j['file_path']}", stream=True, timeout=600)
        last = 0
        total = int(r.headers.get("Content-Length", 0))
        written = 0
        with open(local, "wb") as out:
            for chunk in r.iter_content(64 * 1024):
                if not chunk:
                    continue
                out.write(chunk)
                written += len(chunk)
                now = time.time()
                if now - last > 1.5:
                    last = now
                    pct = (written / total * 100) if total else 0
                    bar = progress_bar(written, total)
                    edit(chat, status_msg_id,
                         f"⬇️ {nombre}\n{bar} {pct:.0f}% "
                         f"({human_size(written)}/{human_size(total)})")
    except Exception as e:
        send(chat, f"❌ No pude descargar de Telegram: {e}")
        return
    LAST_FILE[chat] = local
    try:
        lines, n_parts, sub = upload_to_ojs(journal, local, nombre, chat,
                                            status_msg_id)
    except Exception as e:
        send(chat, f"❌ Error subiendo a {journal}: {e}")
        return
    txt = os.path.join(tmp, f"enlaces_{Path(nombre).stem}.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    enlaces = [l for l in lines if not l.startswith("#")]
    kb = [
        [{"text": "📋 Copiar enlaces (ver mensaje siguiente)",
          "callback_data": "copy"}],
        [{"text": "📜 Ver script", "callback_data": "script"},
         {"text": "🛠 Ajustes", "callback_data": "config"}],
    ]
    send_document(chat, txt,
                  caption=f"✅ *{n_parts} partes* subidas a *{journal}* "
                          f"(submission {sub})")
    send(chat, "📋 *Enlaces:*\n" + "\n".join(enlaces) if len(enlaces) <= 8
         else f"📋 {len(enlaces)} enlaces en el .txt adjunto.",
         keyboard=kb)


# --- dispatcher ------------------------------------------------------------


def handle_text(chat, text):
    text = (text or "").strip()
    if text.startswith("/start") or text in ("/menu", "/nube"):
        menu(chat)
        return
    if text.startswith("/script"):
        script_path = HERE / "descargar_enlace.py"
        if script_path.exists():
            send_document(chat, str(script_path),
                         caption="📜 *descargar_enlace.py* para bajar los "
                                 "enlaces en tu PC.")
        else:
            send(chat, "❌ No encuentro `descargar_enlace.py`.")
        return
    if text.startswith("/temp"):
        files = sorted(Path("/tmp").glob("tg_*"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:20]
        if not files:
            send(chat, "✅ No hay temporales en /tmp.")
            return
        msg = "🧹 *Temporales* (últimos 20):\n"
        total = 0
        for p in files:
            sz = p.stat().st_size
            total += sz
            msg += f"• `{p.name}` ({human_size(sz)})\n"
        msg += f"\n*Total:* {human_size(total)}\nUsa /limpiar para borrar."
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
        send(chat, f"🧹 Limpié {n} archivos ({human_size(freed)}).")
        return
    if text.startswith("/config"):
        kb = [[{"text": f"📡 {s}", "callback_data": f"setdef:{s}"}]
              for s in JOURNAL_SLUGS]
        kb.append([{"text": "PART_MB +1", "callback_data": "partmb:1"},
                   {"text": "PART_MB -1", "callback_data": "partmb:-1"},
                   {"text": "PART_MB = 5", "callback_data": "partmb:5"}])
        actual = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
        send(chat,
             f"⚙️ *Ajustes*\n• Revista: `{actual}`\n• PART_MB: `{PART_MB}` "
             f"(límite OJS: 19 MB)\n• Idle: `{IDLE_MIN}` min",
             keyboard=kb)
        return
    if text.startswith("/renombrar"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send(chat, "Uso: `/renombrar nuevo.pdf`")
            return
        p = LAST_FILE.get(chat)
        if not p or not os.path.exists(p):
            send(chat, "❌ No hay archivo reciente.")
            return
        new_path = os.path.join(os.path.dirname(p), parts[1])
        os.rename(p, new_path)
        LAST_FILE[chat] = new_path
        send_document(chat, new_path, caption=f"✅ Renombrado")
        return
    if text.startswith("/recortar"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            send(chat, "Uso: `/recortar 1000000`")
            return
        n = int(parts[1])
        p = LAST_FILE.get(chat)
        if not p or not os.path.exists(p):
            send(chat, "❌ No hay archivo reciente.")
            return
        new_path = p + ".recortado"
        with open(p, "rb") as f, open(new_path, "wb") as g:
            g.write(f.read(n))
        LAST_FILE[chat] = new_path
        send_document(chat, new_path, caption=f"✅ Recortado a {human_size(n)}")
        return
    if text.startswith("/zip"):
        p = LAST_FILE.get(chat)
        if not p or not os.path.exists(p):
            send(chat, "❌ No hay archivo reciente.")
            return
        zip_path = p + ".zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(p, os.path.basename(p))
        LAST_FILE[chat] = zip_path
        send_document(chat, zip_path, caption="✅ Comprimido .zip")
        return
    if text.startswith("/tamano") or text.startswith("/tamaño") or \
            text.startswith("/size"):
        p = LAST_FILE.get(chat)
        if not p or not os.path.exists(p):
            send(chat, "❌ No hay archivo reciente.")
            return
        send(chat, f"📄 `{os.path.basename(p)}`: "
                   f"{human_size(os.path.getsize(p))}")
        return
    if text.startswith("/ayuda") or text == "/help":
        send(chat,
             "*Ayuda*\n"
             "• Envía un archivo/documento/video para subir a OJS por partes.\n"
             "• Si la extensión no está permitida por OJS, la renombro a `.pdf`.\n"
             "• Envía un enlace http(s) para descargarlo y subirlo.\n"
             "• `/script` te envía `descargar_enlace.py`.\n"
             "• `/temp` muestra temporales; `/limpiar` los borra.\n"
             "• `/config` cambia ajustes.\n"
             "• `/renombrar`, `/recortar`, `/zip`, `/size` sobre el último archivo.")
        return
    # URL directa?
    if text.startswith("http://") or text.startswith("https://"):
        status = send(chat, "🔗 Procesando enlace...")
        status_msg_id = status.get("result", {}).get("message_id")
        try:
            local = download_url_direct(text, "/tmp", chat, status_msg_id)
        except Exception as e:
            send(chat, f"❌ Descarga falló: {e}")
            return
        LAST_FILE[chat] = local
        send(chat, f"✅ Descargado: `{os.path.basename(local)}` "
                   f"({human_size(os.path.getsize(local))}).\n"
                   f"Subiendo a `{CHOSEN.get(str(chat), JOURNAL_DEFAULT)}`...")
        journal = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
        try:
            lines, n_parts, sub = upload_to_ojs(
                journal, local, os.path.basename(local), chat, status_msg_id)
        except Exception as e:
            send(chat, f"❌ Error subiendo: {e}")
            return
        txt = f"/tmp/enlaces_{Path(local).stem}.txt"
        with open(txt, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        send_document(chat, txt,
                      caption=f"✅ {n_parts} partes en {journal} (sub {sub})")
        return
    send(chat, "Envíame un enlace http(s), un archivo, o /start")


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


def handle_callback(cb):
    chat = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    data = cb["data"]
    if data == "help":
        send(chat, "Mira /ayuda o /help")
        return
    if data == "script":
        script_path = HERE / "descargar_enlace.py"
        if script_path.exists():
            send_document(chat, str(script_path),
                          caption="📜 *descargar_enlace.py*")
        return
    if data == "temp":
        handle_text(chat, "/temp")
        return
    if data == "config":
        handle_text(chat, "/config")
        return
    if data == "copy":
        send(chat, "📋 Pulsa sobre el .txt adjunto para ver los enlaces y "
                   "copiarlos manualmente.")
        return
    if data.startswith("nube:"):
        slug = data.split(":", 1)[1]
        persist_chosen(chat, slug)
        tg_post("answerCallbackQuery",
                data={"callback_query_id": cb["id"], "text": f"Nube: {slug}"})
        edit(chat, msg_id, f"✅ Nube elegida: *{slug}*. Envíame el archivo.")
        return
    if data.startswith("setdef:"):
        slug = data.split(":", 1)[1]
        persist_chosen(chat, slug)
        tg_post("answerCallbackQuery",
                data={"callback_query_id": cb["id"],
                      "text": f"Por defecto: {slug}"})
        edit(chat, msg_id, f"✅ Revista por defecto: `{slug}`")
        return
    if data.startswith("partmb:"):
        global PART_MB
        op = data.split(":", 1)[1]
        if op == "5":
            PART_MB = 5
        else:
            PART_MB = max(1, min(MAX_PART_MB, PART_MB + int(op)))
        tg_post("answerCallbackQuery",
                data={"callback_query_id": cb["id"],
                      "text": f"PART_MB = {PART_MB}"})
        edit(chat, msg_id, f"✅ PART_MB = *{PART_MB}* MB")


def dispatch(up):
    if "callback_query" in up:
        handle_callback(up["callback_query"])
    elif up.get("message") or up.get("edited_message"):
        handle_message(up["message"] or up["edited_message"])


# --- main loop -------------------------------------------------------------


def main():
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.time()
    # health check inicial: getMe
    me = tg_get("/getMe").json()
    if not me.get("ok"):
        sys.exit(f"Bot no responde a getMe: {me}")
    log.info("Bot iniciado como @%s", me["result"]["username"])

    offset = 0
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"timeout": 25, "offset": offset,
                                     "allowed_updates": json.dumps(
                                         ["message", "edited_message",
                                          "callback_query"])},
                             timeout=60).json()
        except Exception as e:
            log.warning("getUpdates: %s; reintentando en 5s", e)
            time.sleep(5)
            continue
        for up in r.get("result", []):
            offset = up["update_id"] + 1
            try:
                dispatch(up)
            except Exception as e:
                log.exception("dispatch: %s", e)
            LAST_ACTIVITY = time.time()
        # IDLE_MIN=0 deshabilita el timeout (para Render/VPS 24/7)
        if IDLE_MIN > 0 and time.time() - LAST_ACTIVITY > IDLE_MIN * 60:
            log.info("Sin actividad por %d min, cerrando job.", IDLE_MIN)
            return


if __name__ == "__main__":
    main()
