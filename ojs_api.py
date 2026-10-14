"""
API de subida a OJS (Open Journal Systems) - Nube Revista de Cardiología.

Cliente simple para autenticarse y subir archivos a la revista
https://revcardiologia.sld.cu/index.php/revcardiologia mediante el
método oficial del wizard + REST API de OJS 3.3.

Flujo:
  1. login()      -> obtiene cookie de sesión y token CSRF
  2. create()      -> (opcional) crea una submission nueva desde el wizard
  3. upload()      -> POST multipart a /api/v1/submissions/{id}/files
  4. download_url()-> enlace de descarga generado por OJS
"""

import os
import re
import json
import logging
import requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ojs_api")
# El sitio usa certificado no confiable; deshabilitamos la verificación.
requests.packages.urllib3.disable_warnings()


class OJSApi:
    def __init__(self, host, username, password, journal="revcardiologia",
                 genre_id="1", file_stage="2"):
        self.base = f"{host}/index.php/{journal}"
        self.username = username
        self.password = password
        self.genre_id = genre_id      # 1 = Texto del artículo
        self.file_stage = file_stage  # 2 = Submission (envío)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0")
        self.session.verify = False
        self.csrf = None
        self.journal = journal

    def login(self):
        r = self.session.get(f"{self.base}/login")
        m = re.search(r'name="csrfToken"\s+value="([^"]*)"', r.text)
        if not m:
            raise RuntimeError("CSRF no encontrado en la página de login")
        r = self.session.post(f"{self.base}/login/signIn", data={
            "csrfToken": m.group(1), "source": "",
            "username": self.username, "password": self.password,
            "remember": "1",
        })
        if "login" in r.url and "signIn" not in r.url:
            raise RuntimeError("Login fallido: credenciales incorrectas")
        # El CSRF de la sesión se expone en el JS de /submissions
        self._refresh_csrf()
        return "noErrors" in r.text or True

    def _csrf_from(self, text):
        m = re.search(r'"csrfToken":"([^"]+)"', text)
        return m.group(1) if m else None

    def _refresh_csrf(self):
        m = self._csrf_from(self.session.get(
            f"{self.base}/submissions").text)
        if m:
            self.csrf = m

    def create(self, section_id=None, user_group_id=None, locale="es_ES"):
        """Crea una submission nueva vía el wizard y devuelve su id.

        Descubre automáticamente sectionId (Art. Investigación) y userGroupId
        del paso 1, y envía los checkboxes de la lista de verificación (cada
        revista define cuántos y con qué nombre).
        """
        r = self.session.get(
            f"{self.base}/submission/step/1?submissionId=&sectionId=0")
        d = json.loads(r.text)
        content = d["content"]
        m = re.search(r'name="csrfToken" value="([^"]+)"', content)
        csrf = m.group(1) if m else self.csrf
        # Sección: preferir 'Artículo de Investigación'; si no, la 1ª distinta de 0
        secs = re.findall(r'<option value="(\d+)"[^>]*>([^<]+)</option>', content)
        section_id = section_id or (next(
            (s for s, n in secs if "nvestigaci" in n and "original" not in n.lower()),
            next((s for s, _ in secs if s != "0"), None)))
        m = re.search(r'name="userGroupId"[^>]*value="(\d+)"', content)
        user_group_id = user_group_id or (m.group(1) if m else "14")
        data = {"csrfToken": csrf, "locale": locale, "sectionId": section_id,
                "userGroupId": user_group_id, "submissionChecklist": "1",
                "copyrightNoticeAgree": "1", "privacyConsent": "1"}
        # algunas revistas (ej. mil) exigen cada checkbox por separado
        for i in range(len(re.findall(r'id="checklist-\d+"', content))):
            data[f"checklist-{i}"] = "1"
        r = self.session.post(f"{self.base}/submission/saveStep/1", data=data)
        sub = json.loads(r.text).get("submissionId")
        if not sub:
            m = re.search(r"submissionId=(\d+)", r.text)
            sub = m.group(1) if m else None
        if not sub:
            raise RuntimeError("No se pudo crear la submission: " + r.text[:200])
        return sub

    def upload(self, submission_id, filepath, filename=None):
        """Sube un archivo a la submission y devuelve el diccionario de OJS."""
        filename = filename or os.path.basename(filepath)
        self._refresh_csrf()
        r = self.session.post(
            f"{self.base}/api/v1/submissions/{submission_id}/files",
            headers={"X-CSRF-TOKEN": self.csrf},
            files={"file": (filename, open(filepath, "rb"))},
            data={"fileStage": self.file_stage, "genreId": self.genre_id},
        )
        if r.status_code != 200:
            raise RuntimeError(f"Error subiendo: {r.status_code} {r.text[:200]}")
        return r.json()

    def download_url(self, submission_id, submission_file_id, stage_id="1"):
        """Enlace de descarga del archivo (requiere sesión para abrirlo)."""
        host = self.base.split("/index.php")[0]
        return (f"{host}/index.php/{self.journal}/$$$call$$$/api/file/"
                f"file-api/download-file?submissionFileId={submission_file_id}"
                f"&submissionId={submission_id}&stageId={stage_id}")


def cargar_revista(nombre):
    """Devuelve (host, journal) de revistas.json por slug o nombre."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "revistas.json"), encoding="utf-8") as f:
        for r in json.load(f)["revistas"]:
            if nombre.lower() in (r["journal"].lower(), r["nombre"].lower()):
                if not r.get("compatible", True):
                    raise SystemExit(f"{r['nombre']} es OJS {r['versionOJS']}: no compatible")
                return r["host"], r["journal"]
    raise SystemExit(f"Revista no encontrada: {nombre} (mira revistas.json)")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Subir archivo a OJS (revistas cubanas)")
    p.add_argument("--host", default="https://revcardiologia.sld.cu")
    p.add_argument("--journal", default="revcardiologia",
                   help="slug o nombre de la revista (ver revistas.json)")
    p.add_argument("--user", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--file", required=True, help="ruta del archivo a subir")
    p.add_argument("--submission", type=int, help="id de submission (si no, crea una)")
    p.add_argument("--section", default="25")
    p.add_argument("--genre", default="1")
    args = p.parse_args()

    host, journal = cargar_revista(args.journal) if args.journal != \
        "revcardiologia" else (args.host, "revcardiologia")
    api = OJSApi(host, args.user, args.password,
                 journal=journal, genre_id=args.genre)
    api.login()
    log.info("Sesión iniciada.")
    sub = args.submission or api.create(section_id=args.section)
    log.info("Submission: %s", sub)
    res = api.upload(sub, args.file)
    print(json.dumps({"submissionId": sub, **res}, ensure_ascii=False, indent=2))
    print("Descarga:", api.download_url(sub, res["id"]))
