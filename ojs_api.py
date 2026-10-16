"""
API de subida a OJS (Open Journal Systems) - revistas cubanas 3.3.

Cliente robusto para autenticarse, crear submissions, subir archivos,
descargarlos y obtener enlaces. Soporta:
  - Login con CSRF (2 formatos: JSON o atributo HTML value=)
  - Reintentos ante DNS inestable (los *.sld.cu suelen fallar a veces)
  - Subida multipart a /api/v1/submissions/{id}/files
  - Descarga del archivo (requiere sesión iniciada)
  - Listado y borrado de archivos de una submission

Flujo típico:
  1. login()         -> cookie OJSSID + CSRF
  2. create()        -> crea submission nueva y devuelve su id
  3. upload()        -> POST multipart a /api/v1/submissions/{id}/files
  4. download_url()  -> enlace de descarga generado por OJS
  5. download_file() -> descarga binaria del archivo (sesión requerida)
"""

import os
import re
import json
import time
import logging
import requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ojs_api")
# Los sitios *.sld.cu suelen usar certificados autofirmados o caducados.
requests.packages.urllib3.disable_warnings()

# Patrones de CSRF que se ven en OJS 3.3.x (hay dos variantes en la práctica)
_CSRF_JSON = re.compile(r'"csrfToken"\s*:\s*"([^"]+)"')
_CSRF_HTML = re.compile(r'name="csrfToken"\s+value="([^"]+)"')


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

    def login(self, retries=3, backoff=8):
        """Login con CSRF. Reintenta ante DNS inestable o timeouts."""
        last_err = None
        for i in range(retries):
            try:
                r = self.session.get(f"{self.base}/login", timeout=30)
                m = _CSRF_HTML.search(r.text) or _CSRF_JSON.search(r.text)
                if not m:
                    # a veces OJS responde con JSON al GET /login si la sesión
                    # ya está activa; intentar /submissions como fallback
                    r2 = self.session.get(f"{self.base}/submissions", timeout=30)
                    m = _CSRF_JSON.search(r2.text) or _CSRF_HTML.search(r2.text)
                if not m:
                    raise RuntimeError("CSRF no encontrado en la página de login")
                r = self.session.post(f"{self.base}/login/signIn", data={
                    "csrfToken": m.group(1), "source": "",
                    "username": self.username, "password": self.password,
                    "remember": "1",
                }, timeout=30, allow_redirects=True)
                # Detección robusta de login fallido. OJS 3.3 puede responder:
                #   - 302 a /login?source=... (caso A)
                #   - 200 con la página de login de nuevo (caso B, p.ej. SPU)
                final_url = r.url.split("?")[0].rstrip("/")
                cookies = self.session.cookies.get_dict()
                if final_url.endswith("/login") or "/login?" in r.url or \
                   "/login" in r.url and "source=" in r.url:
                    raise RuntimeError("Login fallido: credenciales o permisos")
                # Prueba definitiva: la cookie OJSSID sola no basta; verificar
                # que podemos acceder a /submissions (o /dashboard) sin redirect
                chk = self.session.get(f"{self.base}/submissions",
                                       allow_redirects=False, timeout=20)
                # 200 = autenticado; 302 a /login = no autenticado
                if chk.status_code == 302 and "/login" in (chk.headers.get(
                        "Location", "") + "?source="):
                    raise RuntimeError(
                        "Login rechazado por el servidor (cookie OJSSID sin "
                        "sesión válida). Verifica usuario/password y rol en "
                        f"{self.journal}.")
                self._refresh_csrf()
                if not self.csrf:
                    raise RuntimeError("Login OK pero no se obtuvo CSRF de sesión")
                return True
            except Exception as e:
                last_err = e
                log.warning("%s: login intento %d/%d falló: %s",
                            self.journal, i + 1, retries, e)
                time.sleep(backoff)
        raise RuntimeError(f"login falló tras {retries} intentos: {last_err}")

    def _csrf_from(self, text):
        m = _CSRF_JSON.search(text) or _CSRF_HTML.search(text)
        return m.group(1) if m else None

    def _refresh_csrf(self):
        # OJS 3.3 puede exponer el csrfToken en /submissions como JSON o
        # como atributo HTML value=, según la revista y el rol.
        for path in ("/submissions", "/submission/wizard", "/dashboard"):
            try:
                r = self.session.get(f"{self.base}{path}", timeout=30)
            except Exception:
                continue
            m = self._csrf_from(r.text)
            if m:
                self.csrf = m
                return

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

    def upload(self, submission_id, filepath, filename=None,
               timeout=900, progress_cb=None):
        """Sube un archivo a la submission y devuelve el diccionario de OJS.

        Si se pasa `progress_cb(bytes_sent, total)` se llama periódicamente.
        Renueva CSRF y reintenta hasta 2 veces ante csrfInvalid / 5xx.
        """
        filename = filename or os.path.basename(filepath)
        size = os.path.getsize(filepath)
        self._refresh_csrf()
        if not self.csrf:
            raise RuntimeError("No hay CSRF; ¿login falló?")
        url = f"{self.base}/api/v1/submissions/{submission_id}/files"
        for intento in range(3):
            try:
                # Si se pide progreso, enviamos el archivo por chunks manuales.
                if progress_cb:
                    f = _ProgressFile(filepath, "rb", progress_cb, size)
                else:
                    f = open(filepath, "rb")
                try:
                    r = self.session.post(
                        url,
                        headers={"X-CSRF-TOKEN": self.csrf},
                        files={"file": (filename, f,
                                         "application/octet-stream")},
                        data={"fileStage": self.file_stage,
                              "genreId": self.genre_id},
                        timeout=timeout,
                    )
                finally:
                    f.close()
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (403, 412) and "csrf" in r.text.lower():
                    log.warning("%s: csrfInvalid al subir, refrescando...",
                                self.journal)
                    self._refresh_csrf()
                    continue
                if 500 <= r.status_code < 600:
                    log.warning("%s: HTTP %d al subir, reintentando...",
                                self.journal, r.status_code)
                    time.sleep(5)
                    continue
                raise RuntimeError(f"Error subiendo: HTTP {r.status_code} "
                                   f"{r.text[:300]}")
            except (requests.Timeout, requests.ConnectionError) as e:
                log.warning("%s: red al subir (%s), reintentando...",
                            self.journal, e)
                time.sleep(5)
        raise RuntimeError(f"upload falló tras 3 intentos en {self.journal}")

    def download_url(self, submission_id, submission_file_id, stage_id="1"):
        """Enlace de descarga del archivo (requiere sesión para abrirlo)."""
        host = self.base.split("/index.php")[0]
        return (f"{host}/index.php/{self.journal}/$$$call$$$/api/file/"
                f"file-api/download-file?submissionFileId={submission_file_id}"
                f"&submissionId={submission_id}&stageId={stage_id}")

    def download_file(self, submission_id, submission_file_id,
                      dest_path, stage_id="1", timeout=300):
        """Descarga el archivo al destino indicado. Requiere sesión iniciada.
        Devuelve la ruta final escrita. Lanza RuntimeError si falla.
        """
        url = self.download_url(submission_id, submission_file_id, stage_id)
        r = self.session.get(url, stream=True, timeout=timeout,
                             allow_redirects=True)
        if r.status_code != 200:
            raise RuntimeError(f"descarga HTTP {r.status_code}: {r.text[:200]}")
        ctype = r.headers.get("Content-Type", "")
        if "text/html" in ctype.lower() and "application/pdf" not in ctype.lower():
            # probablemente pantalla de login o roleBasedAccessDenied
            raise RuntimeError(f"descarga no autorizada (HTML inesperado): "
                               f"{r.text[:200]}")
        with open(dest_path, "wb") as out:
            for chunk in r.iter_content(8192):
                out.write(chunk)
        return dest_path

    def list_files(self, submission_id):
        """Lista los archivos de una submission (REST API)."""
        self._refresh_csrf()
        r = self.session.get(
            f"{self.base}/api/v1/submissions/{submission_id}/files",
            headers={"X-CSRF-TOKEN": self.csrf}, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"list_files HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


class _ProgressFile:
    """Wrapper de archivo que reporta bytes enviados a un callback."""
    def __init__(self, path, mode, cb, total):
        self._f = open(path, mode)
        self._cb = cb
        self._total = total
        self._sent = 0
        self._last_report = 0

    def read(self, size=-1):
        chunk = self._f.read(size if size != -1 else -1)
        if chunk:
            self._sent += len(chunk)
            # reportar como máximo 1 vez cada 100 kB para no saturar
            if self._sent - self._last_report >= 100 * 1024 or \
               self._sent == self._total:
                try:
                    self._cb(self._sent, self._total)
                except Exception:
                    pass
                self._last_report = self._sent
        return chunk

    def __getattr__(self, name):
        return getattr(self._f, name)

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


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
