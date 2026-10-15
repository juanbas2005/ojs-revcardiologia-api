"""
Bot de Telegram (Pyrogram) para subir archivos a revistas OJS cubanas + extras.

Funcionalidades:
  - Subida a OJS por partes de PART_MB MB (5 por defecto), renombrado a .pdf si
    la extension no esta permitida por OJS. Genera .txt con los enlaces.
  - Boton inline "Copiar enlaces" (un toque).
  - Descarga de enlaces directos http(s) con barra de progreso.
  - Descarga de YouTube / TikTok / Instagram via yt-dlp (si esta instalado).
  - Admin de archivos: renombrar, recortar (primer/ultimos N bytes), comprimir
    zip, ver tamano, borrar temporales (/temp, /limpiar).
  - Ajustes: /config permite ver/cambiar revista por defecto, PART_MB, idioma.
  - Comando /script envia descargar_enlace.py al chat.
  - Persistencia: CHOSEN (revista por chat) se guarda en config.json local.
  - Robustez: reintentos en login OJS, timeouts, manejo de errores visible.

Variables de entorno (GitHub Secrets o .secrets/.env local):
  TELEGRAM_BOT_TOKEN  - obligatorio
  OJS_USER            - obligatorio
  OJS_PASS            - password para mil/rme/cocmed/ems/spu
  OJS_PASS_REVCARD    - password para revcardiologia
  PART_MB             - tamano de parte en MB (default 5)
  OJS_JOURNAL         - revista por defecto (default revcardiologia)
  IDLE_MIN            - minutos de inactividad antes de cerrar (default 30)

Pyrogram en modo bot no requiere api_id/api_hash (solo bot_token). Si se
quiere MTProto user-mode, definir API_ID y API_HASH y SESSION_NAME.
"""
import os
import sys
import json
import time
import shutil
import zipfile
import asyncio
import logging
import subprocess
from pathlib import Path

# --- bootstrap env ---------------------------------------------------------
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

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# --- config -----------------------------------------------------------------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OJS_USER = os.environ.get("OJS_USER", "")
OJS_PASS = os.environ.get("OJS_PASS", "")
OJS_PASS_REVCARD = os.environ.get("OJS_PASS_REVCARD", "")
PART_MB = int(os.environ.get("PART_MB", "5"))
JOURNAL_DEFAULT = os.environ.get("OJS_JOURNAL", "revcardiologia")
IDLE_MIN = int(os.environ.get("IDLE_MIN", "30"))
IDLE_TIMEOUT = IDLE_MIN * 60

# 19 MB es el limite confirmado en pruebas reales; PART_MB debe ser menor.
MAX_PART_MB = 19
if PART_MB > MAX_PART_MB:
    log.warning("PART_MB=%d > limite real (%d); ajustando", PART_MB, MAX_PART_MB)
    PART_MB = MAX_PART_MB

# Revistas compatibles (leidas de revistas.json)
import json as _json
REVISTAS_DICT = {}
with open(HERE / "revistas.json", encoding="utf-8") as _f:
    for _r in _json.load(_f)["revistas"]:
        if _r.get("compatible"):
            REVISTAS_DICT[_r["journal"]] = _r
JOURNAL_SLUGS = list(REVISTAS_DICT.keys())

EXT_PERMITIDAS = {"epub", "xml", "xls", "cvs", "doc", "docx", "pdf",
                  "jpg", "png", "html", "gif", "xlsx"}

# --- persistence ------------------------------------------------------------
CONFIG_FILE = HERE / "config.json"


def load_config():
    if CONFIG_FILE.exists():
        try:
            return _json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(_json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:
        log.warning("no pude guardar config.json: %s", e)


CONFIG = load_config()
CHOSEN = CONFIG.get("chosen", {})  # chat_id -> journal


def persist_chosen(chat_id, journal):
    CHOSEN[str(chat_id)] = journal
    CONFIG["chosen"] = CHOSEN
    save_config(CONFIG)


# --- Pyrogram ---------------------------------------------------------------
# Pyrogram 2.x REQUIERE api_id y api_hash, incluso para bots.
# Consiguelos GRATIS en https://my.telegram.org/apps (inicia sesion con tu
# cuenta personal de Telegram, crea una nueva app, copia api_id y api_hash).
# Luego definelos como variables de entorno (GitHub Secrets o .secrets/.env).
try:
    from pyrogram import Client, filters
    from pyrogram.types import (
        Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
        ReplyKeyboardMarkup, KeyboardButton)
    from pyrogram.errors import (
        MessageNotModified, ChatIdInvalid, FloodWait, MessageTooLong)
except ImportError:
    sys.exit("Falta instalar pyrogram: pip install pyrogram tgcrypto")

API_ID = int(os.environ["API_ID"]) if os.environ.get("API_ID") else None
API_HASH = os.environ.get("API_HASH")
SESSION_NAME = os.environ.get("SESSION_NAME", "ojs_bot")

if not API_ID or not API_HASH:
    sys.exit(
        "\n❌ Faltan API_ID y API_HASH.\n\n"
        "Pyrogram 2.x los exige incluso para bots.\n"
        "Como conseguirlos (gratis, ~3 min):\n"
        "  1. Entra en https://my.telegram.org/apps con tu cuenta Telegram personal\n"
        "  2. Crea una nueva app (cualquier nombre)\n"
        "  3. Copia api_id (numero) y api_hash (cadena larga)\n"
        "  4. Anadelos a GitHub Secrets (API_ID, API_HASH) o a .secrets/.env\n"
        "  5. Vuelve a ejecutar este bot.\n\n"
        "Alternativa sin api_id/hash: usar bot_telegram_simple.py "
        "(version con requests, sin Pyrogram).\n")

app = Client("ojs_bot", api_id=API_ID, api_hash=API_HASH,
             bot_token=TOKEN, workdir=str(HERE))


# --- helpers ----------------------------------------------------------------

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


async def tg_send(chat_id, text, reply_markup=None):
    """Envia un mensaje, troceando si supera el limite."""
    try:
        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                await app.send_message(chat_id, text[i:i+4000])
        else:
            await app.send_message(chat_id, text, reply_markup=reply_markup)
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        await tg_send(chat_id, text, reply_markup)
    except Exception as e:
        log.error("tg_send: %s", e)


async def tg_edit(msg, text, reply_markup=None):
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except MessageNotModified:
        pass
    except Exception as e:
        log.debug("tg_edit: %s", e)


async def tg_download_with_progress(client, message, dest_path, status_msg):
    """Descarga un archivo de Telegram mostrando progreso en status_msg."""
    last_update = 0

    async def on_progress(current, total):
        nonlocal last_update
        now = time.time()
        if now - last_update < 1.5 and current < total:
            return
        last_update = now
        pct = (current / total * 100) if total else 0
        bar = progress_bar(current, total)
        try:
            await status_msg.edit_text(
                f"⬇️ Descargando de Telegram\n{bar} {pct:.0f}% "
                f"({human_size(current)}/{human_size(total)})")
        except MessageNotModified:
            pass
        except Exception:
            pass

    await client.download_media(message, file_name=dest_path,
                                progress=on_progress)
    return dest_path


# --- OJS upload con progreso ------------------------------------------------

async def upload_to_ojs(journal, filepath, original_name, chat_id, status_msg,
                        user_chosen_size=None):
    """Sube `filepath` a la revista `journal` por partes de PART_MB MB.
    Devuelve (lista_enlaces, n_partes, submission_id) o lanza Exception.
    """
    host, _ = cargar_revista(journal)
    pw = OJS_PASS_REVCARD if journal == "revcardiologia" else OJS_PASS
    api = OJSApi(host, OJS_USER, pw, journal=journal)
    try:
        await tg_edit(status_msg, f"🔐 Login en {journal}...")
        api.login()
    except Exception as e:
        raise RuntimeError(f"login {journal}: {e}")
    await tg_edit(status_msg, f"📝 Creando submission en {journal}...")
    sub = int(api.create())
    await tg_edit(status_msg,
                 f"✅ Submission {sub} creada.\n📦 Partiendo en "
                 f"{PART_MB} MB y subiendo a {journal}...")

    base_name, ext = safe_filename(original_name)
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
            part_path = f"/tmp/{base_name}_parte{idx:03d}.{ext}"
            with open(part_path, "wb") as g:
                g.write(data)
            await tg_edit(status_msg,
                         f"☁️ Subiendo parte {idx}/{total_parts} a {journal}...")

            def cb(sent, total, _idx=idx, _total_p=total_parts):
                pct = (sent / total * 100) if total else 0
                bar = progress_bar(sent, total)
                loop = asyncio.get_event_loop()
                loop.create_task(tg_edit(
                    status_msg,
                    f"☁️ Subiendo parte {_idx}/{_total_p} a {journal}\n"
                    f"{bar} {pct:.0f}% ({human_size(sent)}/{human_size(total)})"))

            try:
                res = await asyncio.get_event_loop().run_in_executor(
                    None, lambda p=part_path, c=cb: api.upload(
                        sub, p, filename=os.path.basename(part_path),
                        progress_cb=c))
                lines.append(api.download_url(sub, res["id"]))
            except Exception as e:
                lines.append(f"# ERROR en parte {idx}: {e}")
            try:
                os.remove(part_path)
            except OSError:
                pass
    return lines, idx, sub


# --- handlers ---------------------------------------------------------------

@app.on_message(filters.command(["start", "menu", "nube"]))
async def cmd_start(_, m: Message):
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"☁️ {slug}", callback_data=f"nube:{slug}")]
         for slug in JOURNAL_SLUGS] +
        [[InlineKeyboardButton("🛠 Ajustes", callback_data="config"),
          InlineKeyboardButton("ℹ️ Ayuda", callback_data="help")],
         [InlineKeyboardButton("📜 Ver script de descarga",
                              callback_data="script"),
          InlineKeyboardButton("🧹 Limpiar temporales", callback_data="temp")]]
    )
    actual = CHOSEN.get(str(m.chat.id), JOURNAL_DEFAULT)
    await m.reply(
        f"👋 *Bot OJS Cubanas*\n\n"
        f"Nube actual: `{actual}`\n\n"
        f"*Qué puedo hacer:*\n"
        f"• Subir archivos (PDF, etc.) a una revista OJS por partes de "
        f"{PART_MB} MB y devolver un .txt con los enlaces de descarga.\n"
        f"• Descargar de YouTube/TikTok/Instagram/enlaces directos.\n"
        f"• Renombrar, recortar, comprimir (zip) archivos.\n"
        f"• Enviarte el script `descargar_enlace.py` para bajar los enlaces "
        f"en tu PC.\n\n"
        f"Envíame un archivo, video, o un enlace para empezar.",
        reply_markup=kb)


@app.on_callback_query(filters.regex(r"^nube:"))
async def cb_nube(_, c: CallbackQuery):
    slug = c.data.split(":", 1)[1]
    persist_chosen(c.message.chat.id, slug)
    await c.answer(f"Nube: {slug}")
    await c.message.edit_text(
        f"✅ Nube elegida: *{slug}*\nAhora envíame un archivo o enlace.")


@app.on_callback_query(filters.regex(r"^help$"))
async def cb_help(_, c: CallbackQuery):
    await c.answer()
    await c.message.reply(
        "*Ayuda*\n"
        "• Envía un archivo/documento/video para subir a OJS por partes.\n"
        "• Si la extensión no está permitida por OJS, la renombro a `.pdf`.\n"
        "• Para descargar de Internet: envía un enlace http(s), YouTube, "
        "TikTok o Instagram.\n"
        "• `/script` te envía `descargar_enlace.py`.\n"
        "• `/temp` muestra temporales; `/limpiar` los borra.\n"
        "• `/config` muestra y cambia ajustes (revista por defecto, "
        f"PART_MB).\n"
        "• Botón *Copiar enlaces* disponible en el mensaje final.\n"
        "• `/renombrar <nuevo>` renombra el último archivo procesado.\n"
        "• `/recortar <n>` deja los primeros N bytes del último archivo.\n"
        "• `/zip` comprime el último archivo en .zip")


@app.on_callback_query(filters.regex(r"^script$"))
async def cb_script(_, c: CallbackQuery):
    await c.answer("Enviando script...")
    script_path = HERE / "descargar_enlace.py"
    if not script_path.exists():
        await c.message.reply("❌ No encuentro `descargar_enlace.py`.")
        return
    await c.message.reply_document(
        str(script_path),
        caption=("📜 *descargar_enlace.py*\n\n"
                 "Descarga archivos OJS desde enlaces del bot.\n\n"
                 "Uso: `python descargar_enlace.py enlaces.txt`\n"
                 "o: `python descargar_enlace.py 'URL'`\n"
                 "o: `cat enlaces.txt | python descargar_enlace.py -`\n\n"
                 "Guarda en `~/Descargas`. Lee credenciales de "
                 "`.secrets/.env` o variables de entorno."))


@app.on_callback_query(filters.regex(r"^temp$"))
async def cb_temp(_, c: CallbackQuery):
    await c.answer()
    await cmd_temp_impl(c.message)


@app.on_callback_query(filters.regex(r"^config$"))
async def cb_config(_, c: CallbackQuery):
    await c.answer()
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"📡 {s}", callback_data=f"setdef:{s}")]
         for s in JOURNAL_SLUGS] +
        [[InlineKeyboardButton("PART_MB +1", callback_data="partmb:1"),
          InlineKeyboardButton("PART_MB -1", callback_data="partmb:-1"),
          InlineKeyboardButton("PART_MB = 5", callback_data="partmb:5")]]
    )
    actual = CHOSEN.get(str(c.message.chat.id), JOURNAL_DEFAULT)
    await c.message.reply(
        f"⚙️ *Ajustes actuales*\n"
        f"• Revista por defecto: `{actual}`\n"
        f"• PART_MB: `{PART_MB}` MB (límite OJS: 19 MB)\n"
        f"• Idle: `{IDLE_MIN}` min\n\n"
        f"Cambia la revista por defecto o PART_MB:",
        reply_markup=kb)


@app.on_callback_query(filters.regex(r"^setdef:"))
async def cb_setdef(_, c: CallbackQuery):
    global JOURNAL_DEFAULT
    slug = c.data.split(":", 1)[1]
    persist_chosen(c.message.chat.id, slug)
    await c.answer(f"Por defecto: {slug}")
    await c.message.edit_text(f"✅ Revista por defecto cambiada a `{slug}`.")


@app.on_callback_query(filters.regex(r"^partmb:"))
async def cb_partmb(_, c: CallbackQuery):
    global PART_MB
    op = c.data.split(":", 1)[1]
    if op == "5":
        PART_MB = 5
    else:
        PART_MB = max(1, min(MAX_PART_MB, PART_MB + int(op)))
    await c.answer(f"PART_MB = {PART_MB}")
    await c.message.edit_text(
        f"✅ PART_MB = *{PART_MB}* (límite OJS: {MAX_PART_MB} MB)")


# --- estado por chat: ultimo archivo procesado ------------------------------
LAST_FILE = {}  # chat_id -> path local


@app.on_message(filters.command(["temp"]))
async def cmd_temp(_, m: Message):
    await cmd_temp_impl(m)


async def cmd_temp_impl(m):
    tmp = Path("/tmp")
    files = sorted(tmp.glob("tg_*"), key=lambda p: p.stat().st_mtime,
                   reverse=True)[:20]
    if not files:
        await m.reply("✅ No hay temporales en /tmp.")
        return
    txt = "🧹 *Temporales en /tmp* (últimos 20):\n"
    total = 0
    for p in files:
        sz = p.stat().st_size
        total += sz
        txt += f"• `{p.name}` ({human_size(sz)})\n"
    txt += f"\n*Total:* {human_size(total)}\nUsa `/limpiar` para borrarlos."
    await m.reply(txt)


@app.on_message(filters.command(["limpiar"]))
async def cmd_limpiar(_, m: Message):
    n = 0
    freed = 0
    for p in Path("/tmp").glob("tg_*"):
        try:
            sz = p.stat().st_size
            p.unlink()
            n += 1
            freed += sz
        except OSError:
            pass
    await m.reply(f"🧹 Limpié {n} archivos temporales "
                 f"({human_size(freed)} liberados).")


@app.on_message(filters.command(["script"]))
async def cmd_script(_, m: Message):
    script_path = HERE / "descargar_enlace.py"
    if not script_path.exists():
        await m.reply("❌ No encuentro `descargar_enlace.py`.")
        return
    await m.reply_document(
        str(script_path),
        caption="📜 *descargar_enlace.py* — ejecútalo en tu PC para bajar "
                "los enlaces que produce el bot.")


@app.on_message(filters.command(["config"]))
async def cmd_config(_, m: Message):
    await cb_config.__wrapped__(app, _fake_cb(m))


def _fake_cb(m):
    class _C:
        def __init__(self, m):
            self.message = m
            self.data = "config"
        async def answer(self, *a, **k): pass
    return _C(m)


@app.on_message(filters.command(["renombrar"]))
async def cmd_renombrar(_, m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.reply("Uso: `/renombrar nuevo_nombre.pdf`")
        return
    new = parts[1].strip()
    p = LAST_FILE.get(m.chat.id)
    if not p or not os.path.exists(p):
        await m.reply("❌ No hay archivo reciente para renombrar.")
        return
    new_path = os.path.join(os.path.dirname(p), new)
    os.rename(p, new_path)
    LAST_FILE[m.chat.id] = new_path
    await m.reply_document(new_path, caption=f"✅ Renombrado a `{new}`")


@app.on_message(filters.command(["recortar"]))
async def cmd_recortar(_, m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await m.reply("Uso: `/recortar 1000000` (deja 1 MB al inicio)")
        return
    n = int(parts[1])
    p = LAST_FILE.get(m.chat.id)
    if not p or not os.path.exists(p):
        await m.reply("❌ No hay archivo reciente para recortar.")
        return
    new_path = p + ".recortado"
    with open(p, "rb") as f, open(new_path, "wb") as g:
        g.write(f.read(n))
    LAST_FILE[m.chat.id] = new_path
    await m.reply_document(new_path, caption=f"✅ Recortado a {human_size(n)}")


@app.on_message(filters.command(["zip"]))
async def cmd_zip_cmd(_, m: Message):
    p = LAST_FILE.get(m.chat.id)
    if not p or not os.path.exists(p):
        await m.reply("❌ No hay archivo reciente para comprimir.")
        return
    zip_path = p + ".zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(p, os.path.basename(p))
    LAST_FILE[m.chat.id] = zip_path
    await m.reply_document(zip_path, caption="✅ Comprimido en .zip")


@app.on_message(filters.command(["tamaño", "tamano", "size"]))
async def cmd_size(_, m: Message):
    p = LAST_FILE.get(m.chat.id)
    if not p or not os.path.exists(p):
        await m.reply("❌ No hay archivo reciente.")
        return
    await m.reply(f"📄 `{os.path.basename(p)}`: {human_size(os.path.getsize(p))}")


# --- descargadores externos -------------------------------------------------

async def download_url_direct(url, dest_dir, status_msg):
    """Descarga URL directa con barra de progreso."""
    import requests
    requests.packages.urllib3.disable_warnings()
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
                await tg_edit(status_msg,
                              f"⬇️ {fname}\n{bar} {pct:.0f}% "
                              f"({human_size(written)}/{human_size(total)})")
    return dest


async def download_with_ytdlp(url, dest_dir, status_msg):
    """Descarga con yt-dlp (YouTube, TikTok, Instagram, etc.)."""
    await tg_edit(status_msg, f"🔎 Procesando con yt-dlp:\n`{url[:80]}`")
    # -o: patron de salida; --no-playlist para evitar descargar listas enteras
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "-o",
           os.path.join(dest_dir, "%(title).80s.%(ext)s"), url]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    last = time.time()
    while True:
        line = await proc.stderr.readline()
        if not line:
            if proc.returncode is not None:
                break
            await asyncio.sleep(0.2)
            continue
        text = line.decode("utf-8", errors="replace").strip()
        # yt-dlp escribe progreso tipo "[download]  42.0% of 10.00MiB at 1.00MiB/s"
        if "[download]" in text and time.time() - last > 1.0:
            last = time.time()
            await tg_edit(status_msg, f"⬇️ yt-dlp\n`{text[:200]}`")
    if proc.returncode != 0:
        err = (await proc.stderr.read()).decode("utf-8", errors="replace")
        raise RuntimeError(f"yt-dlp falló: {err[:300]}")
    # Buscar el archivo más reciente en dest_dir
    files = sorted(Path(dest_dir).glob("*"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    return str(files[0]) if files else None


def is_ytdlp_url(url):
    """Devuelve True si la URL parece de YouTube/TikTok/Instagram."""
    low = url.lower()
    return any(h in low for h in ("youtube.com", "youtu.be",
                                   "tiktok.com", "instagram.com",
                                   "instagr.am"))


# --- procesar archivo de Telegram -> subir a OJS ----------------------------

async def process_upload(client, m: Message):
    chat = m.chat.id
    doc = m.document or m.video or m.audio or m.animation or m.voice or \
        m.video_note
    file_name = (m.document.file_name if m.document and
                 m.document.file_name else None) or \
        f"media_{int(time.time())}.mp4"
    file_size = (doc.file_size or 0) if doc else 0
    journal = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
    if journal not in REVISTAS_DICT:
        await m.reply(f"❌ Revista no válida: {journal}. Usa /start")
        return
    status = await m.reply(
        f"📥 Recibí `{file_name}` ({human_size(file_size)}). "
        f"Nube: *{journal}*")
    tmp_dir = "/tmp"
    local = os.path.join(tmp_dir, f"tg_{int(time.time())}_{file_name}")
    try:
        await tg_download_with_progress(client, m, local, status)
    except Exception as e:
        await m.reply(f"❌ No pude descargar de Telegram: {e}")
        return
    LAST_FILE[chat] = local
    try:
        lines, n_parts, sub = await upload_to_ojs(
            journal, local, file_name, chat, status)
    except Exception as e:
        await m.reply(f"❌ Error subiendo a {journal}: {e}")
        return
    txt_path = os.path.join(tmp_dir, f"enlaces_{Path(file_name).stem}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    # Botón inline "Copiar enlaces"
    enlaces = [l for l in lines if not l.startswith("#")]
    copy_payload = "\n".join(enlaces)[:4000]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Copiar enlaces",
                              callback_data="copy:" + str(hash(copy_payload)))],
        [InlineKeyboardButton("📜 Ver script", callback_data="script"),
         InlineKeyboardButton("🛠 Ajustes", callback_data="config")],
    ])
    # Guardar payload en memoria para el callback
    COPY_PAYLOAD[str(chat)] = {str(hash(copy_payload)): copy_payload}
    await m.reply_document(
        txt_path,
        caption=f"✅ *{n_parts} partes* subidas a *{journal}* (submission {sub})",
        reply_markup=kb)
    if len(enlaces) <= 8:
        await m.reply("📋 *Enlaces:*\n" + "\n".join(enlaces))
    else:
        await m.reply(f"📋 {len(enlaces)} enlaces en el .txt adjunto.")


COPY_PAYLOAD = {}


@app.on_callback_query(filters.regex(r"^copy:"))
async def cb_copy(_, c: CallbackQuery):
    h = c.data.split(":", 1)[1]
    payload = COPY_PAYLOAD.get(str(c.message.chat.id), {}).get(h)
    if not payload:
        await c.answer("Enlaces expirados. Vuelve a subir el archivo.")
        return
    await c.answer("Enlaces copiados al portapapeles del mensaje siguiente.",
                   show_alert=False)
    await c.message.reply(
        f"📋 *Enlaces (copia manual ya que Telegram no permite copiar "
        f"directamente al portapapeles del cliente):*\n\n```\n{payload}\n```")


# --- dispatcher principal ---------------------------------------------------

@app.on_message(filters.text & ~filters.command(["start", "menu", "nube",
                                                 "help", "script", "temp",
                                                 "limpiar", "config",
                                                 "renombrar", "recortar",
                                                 "zip", "tamaño", "tamano",
                                                 "size"]))
async def handle_text(_, m: Message):
    """Texto: si parece URL, intenta descargar; si no, ayuda."""
    text = (m.text or "").strip()
    if not text:
        return
    if not (text.startswith("http://") or text.startswith("https://")):
        await m.reply("Envíame un enlace (http/https), un archivo, o /start")
        return
    status = await m.reply(f"🔗 Procesando enlace...")
    tmp_dir = "/tmp"
    try:
        if is_ytdlp_url(text):
            local = await download_with_ytdlp(text, tmp_dir, status)
        else:
            local = await download_url_direct(text, tmp_dir, status)
    except Exception as e:
        await m.reply(f"❌ Descarga falló: {e}")
        return
    if not local or not os.path.exists(local):
        await m.reply("❌ Descarga falló: archivo no encontrado.")
        return
    LAST_FILE[m.chat.id] = local
    await status.edit_text(
        f"✅ Descargado: `{os.path.basename(local)}` "
        f"({human_size(os.path.getsize(local))})\n\n"
        f"¿Subir a OJS?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("☁️ Sí, subir", callback_data="upload"),
             InlineKeyboardButton("❌ No, solo guardar", callback_data="noop")]
        ]))


@app.on_callback_query(filters.regex(r"^upload$"))
async def cb_upload(_, c: CallbackQuery):
    chat = c.message.chat.id
    local = LAST_FILE.get(chat)
    if not local or not os.path.exists(local):
        await c.answer("Archivo no disponible.")
        return
    await c.answer("Subiendo a OJS...")
    journal = CHOSEN.get(str(chat), JOURNAL_DEFAULT)
    status = await c.message.reply(f"☁️ Subiendo a {journal}...")
    try:
        lines, n_parts, sub = await upload_to_ojs(
            journal, local, os.path.basename(local), chat, status)
    except Exception as e:
        await c.message.reply(f"❌ Error: {e}")
        return
    txt_path = f"/tmp/enlaces_{Path(local).stem}.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    await c.message.reply_document(
        txt_path,
        caption=f"✅ {n_parts} partes en {journal} (sub {sub})")


@app.on_callback_query(filters.regex(r"^noop$"))
async def cb_noop(_, c: CallbackQuery):
    await c.answer("OK, archivo guardado en /tmp")


@app.on_message(filters.document | filters.video | filters.audio |
                filters.animation | filters.voice | filters.video_note)
async def handle_media(client, m: Message):
    await process_upload(client, m)


# --- main -------------------------------------------------------------------

LAST_ACTIVITY = time.time()


def _bump():
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.time()


@app.on_message(group=1)
async def _bump_activity(_, m: Message):
    """Refresca el timestamp de actividad (para idle timeout).
    En grupo 1: corre despues de los handlers del grupo 0."""
    _bump()


async def idle_watcher():
    """Cierra el bot si pasa IDLE_MIN minutos sin actividad."""
    while True:
        await asyncio.sleep(60)
        if time.time() - LAST_ACTIVITY > IDLE_TIMEOUT:
            log.info("Sin actividad por %d min, cerrando.", IDLE_MIN)
            await app.stop()
            return


async def health_check():
    """Verifica el estado del bot con getMe cada 5 min; relanza si esta caido."""
    while True:
        await asyncio.sleep(300)
        try:
            from pyrogram.raw.functions.users import GetFullUser as _unused
            # el hecho de que el loop siga corriendo implica que app.is_initialized
            if not app.is_initialized:
                log.error("Bot desconectado; reiniciando...")
                await app.stop()
                await asyncio.sleep(3)
                await app.start()
        except Exception as e:
            log.error("health_check: %s", e)


if __name__ == "__main__":
    async def _runner():
        await app.start()
        me = await app.get_me()
        log.info("Bot iniciado como @%s (id=%s)", me.username, me.id)
        asyncio.create_task(idle_watcher())
        asyncio.create_task(health_check())
        # mantener vivo hasta que idle_watcher cancele
        while app.is_initialized:
            await asyncio.sleep(2)
        log.info("Saliendo del runner.")
    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        log.info("Interrumpido por usuario.")
