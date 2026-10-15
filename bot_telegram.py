"""
Bot de Telegram: recibe un archivo, lo parte en trozos y lo sube a una
revista OJS cubana (la "nube") elegida con botones, devolviendo un .txt
con todos los enlaces de descarga. Corre en GitHub Actions.
El token y las credenciales vienen de los secrets, nunca del repo.
"""
import os
import sys
import time
import json
import shutil

import requests
requests.packages.urllib3.disable_warnings()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ojs_api import OJSApi, cargar_revista  # noqa: E402

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OJS_USER = os.environ["OJS_USER"]
OJS_PASS = os.environ["OJS_PASS"]                 # mil/rme/cocmed/ems
OJS_PASS_REVCARD = os.environ["OJS_PASS_REVCARD"]
PART_MB = int(os.environ.get("PART_MB", "5"))
JOURNAL_DEFAULT = os.environ.get("OJS_JOURNAL", "revcardiologia")
JOURNAL_SLUGS = ["revcardiologia", "mil", "rme", "cocmed", "ems"]
API = f"https://api.telegram.org/bot{TOKEN}"
EXT_PERMITIDAS = {"epub", "xml", "xls", "cvs", "doc", "docx", "pdf",
                  "jpg", "png", "html", "gif", "xlsx"}
CHOSEN = {}   # chat_id -> revista elegida
offset = 0


def tg_get(url, **kw):
    return requests.get(f"{API}{url}", timeout=900, **kw)


def tg_post(method, data=None, files=None):
    return requests.post(f"{API}/{method}", data=data, files=files, timeout=900)


def send(chat, text, keyboard=None):
    data = {"chat_id": chat, "text": text}
    if keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    tg_post("sendMessage", data=data)


def send_file(chat, path, caption=""):
    with open(path, "rb") as f:
        tg_post("sendDocument", data={"chat_id": chat, "caption": caption},
                files={"document": (os.path.basename(path), f)})


def menu(chat):
    kb = [[{"text": f"☁️ {s}", "callback_data": s}] for s in JOURNAL_SLUGS]
    kb.append([{"text": "ℹ️ Ayuda", "callback_data": "help"}])
    actual = CHOSEN.get(chat, JOURNAL_DEFAULT)
    send(chat, f"Elige la nube (revista). Actual: **{actual}**", keyboard=kb)


def procesar(chat, file_id, nombre, size):
    base = os.path.splitext(nombre)[0]
    ext = os.path.splitext(nombre)[1].lstrip(".").lower()
    journal = CHOSEN.get(chat, JOURNAL_DEFAULT)

    # OJS solo acepta unas extensiones: renombramos a .pdf cualquier otra
    # (video, audio, etc.) para que la revista lo admita.
    if ext not in EXT_PERMITIDAS:
        send(chat, f"ℹ️ «{nombre}» es tipo **.{ext or '?'}** (no permitido); "
                   f"lo renombro a **.pdf** para que la revista lo acepte.")
        nombre = base + ".pdf"

    send(chat, f"Recibí «{nombre}» ({size} bytes). Partiendo en "
               f"{PART_MB} MB y subiendo a **{journal}**...")

    host, _ = cargar_revista(journal)
    password = OJS_PASS_REVCARD if journal == "revcardiologia" else OJS_PASS
    api = OJSApi(host, OJS_USER, password, journal=journal)
    try:
        api.login()
        sub = int(api.create())
    except Exception as e:
        send(chat, f"Fallo en {journal}: {e}")
        return

    tmp = "/tmp"
    local = os.path.join(tmp, f"tg_{int(time.time())}_{nombre}")
    try:
        j = tg_get("/getFile", params={"file_id": file_id}).json()["result"]
        r = tg_get(f"/{j['file_path']}", stream=True)
        with open(local, "wb") as out:
            shutil.copyfileobj(r.raw, out)
    except Exception as e:
        send(chat, f"Fallo al descargar de Telegram: {e}")
        return

    lines = [f"# «{nombre}» ({size} bytes), {PART_MB} MB por parte",
             f"# Submission {sub} en {journal}"]
    idx = 0
    with open(local, "rb") as f:
        while True:
            data = f.read(PART_MB * 1024 * 1024)
            if not data:
                break
            idx += 1
            part = os.path.join(tmp, f"{base}_parte{idx:03d}{os.path.splitext(nombre)[1]}")
            with open(part, "wb") as g:
                g.write(data)
            try:
                res = api.upload(sub, part)
                lines.append(api.download_url(sub, res["id"]))
            except Exception as e:
                lines.append(f"# ERROR en parte {idx}: {e}")
    txt = os.path.join(tmp, f"enlaces_{base}.txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    send_file(chat, txt, caption=f"Enlaces de «{nombre}» ({idx} partes) en {journal}")
    send(chat, "\n".join(lines) if idx <= 12 else
         f"{idx} partes listas en el .txt adjunto.",
         keyboard=None)


def handle_message(msg):
    chat = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if text.startswith("/start") or text in ("/nube", "/menu"):
        menu(chat)
        return
    doc = msg.get("document")
    if doc:                                   # archivos / documentos
        procesar(chat, doc["file_id"], doc.get("file_name", "archivo"),
                 doc["file_size"])
        return
    vid = msg.get("video") or msg.get("video_note") or msg.get("audio") or \
         msg.get("voice") or msg.get("animation")
    if vid:  # multimedia -> OJS la rechazará por extensión
        procesar(chat, vid["file_id"], vid.get("file_name") or
                 f"media_{int(time.time())}.mp4", vid.get("file_size", 0))
        return
    send(chat, "Envíame un documento (o toca /start para ver las nubes).",
         keyboard=None)


def handle_callback(cb):
    chat = cb["message"]["chat"]["id"]
    data = cb["data"]
    msg = cb["message"]["message_id"]
    if data == "help":
        send(chat, "Envía un archivo o video para partir y subir; lo renombro "
                   "a .pdf si hace falta. Elige la nube con los botones. "
                   "(/start para el menú)")
        return
    CHOSEN[chat] = data
    tg_post("answerCallbackQuery",
            data={"callback_query_id": cb["id"], "text": f"Nube: {data}"})
    send(chat, f"Listo. Nube elegida: **{data}**. Envíame el archivo.",
         keyboard=None)
    tg_post("editMessageReplyMarkup",
            data={"chat_id": chat, "message_id": msg, "reply_markup":
                  json.dumps({"inline_keyboard": []})})


def dispatch(up):
    if "callback_query" in up:
        handle_callback(up["callback_query"])
    elif up.get("message") or up.get("edited_message"):
        handle_message(up["message"] or up["edited_message"])


def main():
    global offset
    while True:
        try:
            r = requests.get(f"{API}/getUpdates",
                             params={"timeout": 25, "offset": offset},
                             timeout=60).json()
        except Exception:
            time.sleep(5)
            continue
        for up in r.get("result", []):
            offset = up["update_id"] + 1   # confirmar -> no re-entregar
            dispatch(up)
        if time.time() > start + int(os.environ.get("IDLE_MIN", "30")) * 60:
            print("Sin actividad; cerrando el job de Actions.")
            return


start = time.time()
if __name__ == "__main__":
    main()
