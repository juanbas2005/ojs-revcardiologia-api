"""
Bot de Telegram que recibe archivos, los parte en trozos y los sube a una
revista OJS cubana (la "nube"), devolviendo un .txt con todos los enlaces
de descarga. Corre en GitHub Actions (workflow_dispatch). El token del bot
y las credenciales vienen de los secrets, nunca del repo.
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
JOURNAL_SLUGS = {"revcardiologia", "mil", "rme", "cocmed", "ems"}
API = f"https://api.telegram.org/bot{TOKEN}"


def tg_get(url, **kw):
    return requests.get(f"{API}{url}", timeout=900, **kw)


def tg_post(method, data=None, files=None):
    return requests.post(f"{API}/{method}", data=data, files=files, timeout=900)


def send(chat, text):
    tg_post("sendMessage", data={"chat_id": chat, "text": text})


def send_file(chat, path, caption=""):
    with open(path, "rb") as f:
        tg_post("sendDocument", data={"chat_id": chat, "caption": caption},
                files={"document": (os.path.basename(path), f)})


def handle_document(msg):
    chat = msg["chat"]["id"]
    doc = msg["document"]
    name = doc.get("file_name", "archivo")
    size = doc.get("file_size", 0)
    send(chat, f"Recibí «{name}» ({size} bytes). Procesando...")

    journal = next((w for w in msg.get("caption", "").split() if w in
                    JOURNAL_SLUGS), JOURNAL_DEFAULT)
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
    base = os.path.splitext(name)[0]
    local = os.path.join(tmp, f"tg_{int(time.time())}_{name}")
    try:
        j = tg_get("/getFile", params={"file_id": doc["file_id"]}).json()["result"]
        p = j["file_path"]
        r = tg_get(f"/{p}", stream=True)
        with open(local, "wb") as out:
            shutil.copyfileobj(r.raw, out)
    except Exception as e:
        send(chat, f"Fallo al descargar de Telegram: {e}")
        return

    ext = os.path.splitext(name)[1]
    lines = []
    lines.append(f"# «{name}» ({size} bytes), {PART_MB} MB por parte")
    lines.append(f"# Submission {sub} en {journal}")
    idx = 0
    with open(local, "rb") as f:
        while True:
            data = f.read(PART_MB * 1024 * 1024)
            if not data:
                break
            idx += 1
            part = os.path.join(tmp, f"{base}_parte{idx:03d}{ext}")
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
    send_file(chat, txt, caption=f"Enlaces de «{name}» ({idx} partes) en {journal}")
    send(chat, "\n".join(lines) if idx <= 12 else
         f"{idx} partes listas en el .txt adjunto.")


def handle_update(u):
    msg = u.get("message") or u.get("edited_message")
    if not msg:
        return
    chat = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
        send(chat, "Hola. Envíame un archivo y lo subiré a la nube (revista "
                   "OJS) partido en trozos de 5 MB con un .txt de enlaces. "
                   "Puedes nombrar la revista: revcardiologia, mil, rme, "
                   "cocmed, ems.")
        return
    if "document" in msg:
        handle_document(msg)


def main():
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"timeout": 25},
                             timeout=60).json()
        except Exception:
            time.sleep(5)
            continue
        for up in r.get("result", []):
            handle_update(up)
        if time.time() > start + int(os.environ.get("IDLE_MIN", "30")) * 60:
            print("Sin actividad; cerrando el job de Actions.")
            return


start = time.time()
if __name__ == "__main__":
    main()
