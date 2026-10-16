#!/usr/bin/env python3
"""
descargar_enlace.py — Descarga archivos de OJS (revistas cubanas) desde los
enlaces que genera el bot de Telegram, de la forma más fácil posible.

CARACTERÍSTICAS NUEVAS:
  - Autodetección robusta del host/journal desde el enlace:
      * Acepta URLs con $$$call$$$/api/file/file-api/download-file
      * Acepta URLs recortadas (sin $$$call$$$, con /api/v1/submissions/...)
      * Acepta host solo (revcardiologia.sld.cu) + submissionFileId suelto
      * Detecta el slug de la revista desde el hostname o el path
  - Credenciales embebidas en el propio script (NO se suben a git porque
    el script vive en .secrets/ localmente; en GitHub Actions el bot
    provee las creds por env). Si .secrets/.env existe, se usa primero.
  - Modo interactivo: ejecutar sin argumentos y pegar enlaces.
  - Resume descargas parciales (HTTP Range) si el servidor lo soporta.
  - Barra de progreso simple por consola.
  - Colores ANSI suaves (se desactivan si no es TTY).
  - Mensajes claros: qué detectó, qué login hizo, dónde guardó.

USO:
  # 1. Modo interactivo (lo más fácil)
  python descargar_enlace.py

  # 2. Un enlace suelto
  python descargar_enlace.py "https://revcardiologia.sld.cu/index.php/revcardiologia/$$$call$$$/api/file/file-api/download-file?submissionFileId=182084&submissionId=3562&stageId=1"

  # 3. Varios enlaces
  python descargar_enlace.py URL1 URL2 URL3

  # 4. Desde un archivo .txt (una URL por línea; # = comentario)
  python descargar_enlace.py enlaces.txt

  # 5. Desde stdin
  cat enlaces.txt | python descargar_enlace.py -

  # 6. Cambiar directorio destino
  python descargar_enlace.py -o ~/Descargas/MisPDFs URL

  # 7. Pedir usuario/password por prompt (no usar embebidos)
  python descargar_enlace.py --pedir-creds URL

DEPENDENCIAS: solo `requests` (incluida en requirements.txt).
"""
import os
import re
import sys
import time
import argparse
import urllib.parse as up
from pathlib import Path

try:
    import requests
    requests.packages.urllib3.disable_warnings()
except ImportError:
    sys.exit("Falta instalar requests:  pip install requests")

HERE = Path(__file__).resolve().parent

# ===========================================================================
# CREDENCIALES DE LAS NUBES (revistas OJS cubanas)
# ===========================================================================
# Por seguridad, las claves NO viven en este archivo (que se sube a git).
# Se cargan desde (en orden de prioridad):
#   1.Variables de entorno: OJS_USER, OJS_PASS, OJS_PASS_REVCARD
#   2. .secrets/.env local (NO se sube a git; está en .gitignore)
#   3. .secrets/credenciales_ojs.py local (Python dict; NO se sube a git)
#   4. config.json local (NO se sube a git)
#
# Si ninguna fuente tiene credenciales, el script las pedirá por prompt.
# Para crear tu .secrets/credenciales_ojs.py, copia este template:
#
#     # .secrets/credenciales_ojs.py
#     USER = "riroennis"
#     PASSWORDS = {
#         "revcardiologia": "Pupi@#1212",
#         "mil":            "Riroennis@2026",
#         "rme":            "Riroennis@2026",
#         "cocmed":         "Riroennis@2026",
#         "ems":            "Riroennis@2026",
#         "spu":            "Riroennis@2026",
#     }
# ===========================================================================

DEFAULT_USER = "riroennis"  # solo el usuario; las claves vienen de env/local

# Mapeo: hostname -> (host_base, journal_slug)
# Se usa para autodetectar la revista desde el enlace.
HOST_MAP = {
    "revcardiologia.sld.cu":      ("https://revcardiologia.sld.cu",      "revcardiologia"),
    "revmedmilitar.sld.cu":       ("https://revmedmilitar.sld.cu",       "mil"),
    "revmedicaelectronica.sld.cu":("https://revmedicaelectronica.sld.cu", "rme"),
    "revcocmed.sld.cu":           ("https://revcocmed.sld.cu",           "cocmed"),
    "ems.sld.cu":                 ("https://ems.sld.cu",                 "ems"),
    "revsaludpublica.sld.cu":     ("https://revsaludpublica.sld.cu",     "spu"),
    # alias común aunque no resuelva DNS:
    "spu.sld.cu":                 ("https://revsaludpublica.sld.cu",     "spu"),
}

# Passwords cargados desde .secrets/credenciales_ojs.py si existe
_EMBEDDED_PASSWORDS = {}

_cred_file_py = HERE / ".secrets" / "credenciales_ojs.py"
if _cred_file_py.exists():
    try:
        import importlib.util
        _spec = importlib.util.spec_from_file_location("_cred_ojs", _cred_file_py)
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _EMBEDDED_PASSWORDS = getattr(_mod, "PASSWORDS", {})
        if getattr(_mod, "USER", None):
            DEFAULT_USER = _mod.USER
    except Exception as e:
        sys.stderr.write(f"AVISO: no pude cargar .secrets/credenciales_ojs.py: {e}\n")

# Colores ANSI (se desactivan si NO es TTY)
_USE_COLOR = sys.stdout.isatty()


def c(text, color):
    if not _USE_COLOR:
        return text
    codes = {"red": "31", "green": "32", "yellow": "33",
             "blue": "34", "magenta": "35", "cyan": "36",
             "gray": "90", "bold": "1"}
    return f"\033[{codes.get(color, '0')}m{text}\033[0m"


# --- Carga de credenciales desde .secrets/.env o config.json ---------------

def load_env():
    """Carga variables de .secrets/.env y/o config.json si existen.
    Estas variables tienen prioridad sobre las embebidas en DEFAULT_*."""
    env_file = HERE / ".secrets" / ".env"
    if not env_file.exists():
        env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    cfg_file = HERE / "config.json"
    if cfg_file.exists():
        import json
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            for k, v in cfg.items():
                os.environ.setdefault(k, str(v))
        except Exception:
            pass


def get_user():
    """Usuario OJS. Override con --user, OJS_USER, o default."""
    return (os.environ.get("DESCARGAR_USER") or
            os.environ.get("OJS_USER") or DEFAULT_USER)


def get_password_for(journal):
    """Password para la revista. Orden de prioridad:
    1. env OJS_PASS_REVCARD (revcardiologia) o OJS_PASS (demás)
    2. .secrets/credenciales_ojs.py PASSWORDS[journal]
    3. .secrets/.env OJS_PASS_REVCARD / OJS_PASS"""
    if journal == "revcardiologia":
        env_val = os.environ.get("OJS_PASS_REVCARD")
        if env_val:
            return env_val
    else:
        env_val = os.environ.get("OJS_PASS") or os.environ.get("OJS_PASS_DEFAULT")
        if env_val:
            return env_val
    return _EMBEDDED_PASSWORDS.get(journal)


# --- Autodetección robusta de host + journal desde la URL -------------------

# Patrón 1: URL completa con $$$call$$$/api/file/file-api/download-file?params
_RE_FULL = re.compile(
    r"https?://([^/]+)/index\.php/([^/\$]+)(?:/\$\$\$call\$\$\$)?/api/file/file-api/download-file\?([^#\s]+)",
    re.IGNORECASE,
)

# Patrón 2: URL con /api/v1/submissions/{sub}/files/{file_id} (REST directo)
_REST = re.compile(
    r"https?://([^/]+)/index\.php/([^/]+)/api/v1/submissions/(\d+)/files/(\d+)",
    re.IGNORECASE,
)

# Patrón 3: solo host + path con journal visible
_HOSTPATH = re.compile(r"https?://([^/]+)/index\.php/([^/]+)", re.IGNORECASE)

# Patrón 4: extraer submissionFileId y submissionId de cualquier URL
_QS_FILE = re.compile(r"submissionFileId=(\d+)", re.IGNORECASE)
_QS_SUB = re.compile(r"submissionId=(\d+)", re.IGNORECASE)


def detect_from_url(url):
    """Devuelve dict con host, journal, submission_id, file_id, stage_id, original_url.
    Lanza ValueError si no puede detectar nada."""
    if not url or not url.strip():
        raise ValueError("URL vacía")

    url = url.strip().strip("`'\"")

    # 1. Patrón completo: download-file?...
    m = _RE_FULL.search(url)
    if m:
        host_full = "https://" + m.group(1)
        journal = m.group(2)
        qs = up.parse_qs(m.group(3))
        return {
            "host": host_full,
            "journal": journal,
            "submission_id": qs.get("submissionId", ["?"])[0],
            "file_id": qs.get("submissionFileId", ["?"])[0],
            "stage_id": qs.get("stageId", ["1"])[0],
            "url": url,
        }

    # 2. Patrón REST: /api/v1/submissions/{sub}/files/{file_id}
    m = _REST.search(url)
    if m:
        host_full = "https://" + m.group(1)
        journal = m.group(2)
        sub = m.group(3)
        fid = m.group(4)
        return {
            "host": host_full,
            "journal": journal,
            "submission_id": sub,
            "file_id": fid,
            "stage_id": "1",
            "url": url,
            # construir URL de descarga:
            "download_url": (
                f"{host_full}/index.php/{journal}/$$$call$$$/api/file/"
                f"file-api/download-file?submissionFileId={fid}"
                f"&submissionId={sub}&stageId=1"),
        }

    # 3. Host + index.php/journal visible, con submissionFileId en querystring
    m = _HOSTPATH.search(url)
    if m:
        hostname = m.group(1).lower()
        journal_from_path = m.group(2).split("?")[0].split("/")[0]
        sub = _QS_SUB.search(url)
        fid = _QS_FILE.search(url)
        # resolver host vía HOST_MAP
        if hostname in HOST_MAP:
            host_full, journal = HOST_MAP[hostname]
            # si el path tiene un journal distinto al del host, usar el del path
            # (puede pasar con alias spu.sld.cu que mapea a revsaludpublica)
            if journal_from_path and journal_from_path != journal:
                journal = journal_from_path
            return {
                "host": host_full,
                "journal": journal,
                "submission_id": sub.group(1) if sub else "?",
                "file_id": fid.group(1) if fid else "?",
                "stage_id": "1",
                "url": url,
            }
        # si el hostname no está en HOST_MAP pero el journal parece válido,
        # intentar construir el host a ciegas
        if journal_from_path in HOST_MAP.values() or \
           journal_from_path in _EMBEDDED_PASSWORDS:
            return {
                "host": "https://" + hostname,
                "journal": journal_from_path,
                "submission_id": sub.group(1) if sub else "?",
                "file_id": fid.group(1) if fid else "?",
                "stage_id": "1",
                "url": url,
            }

    raise ValueError(f"no pude detectar la revista desde esta URL: {url[:120]}")


# --- Login OJS --------------------------------------------------------------

def login(host, journal, user, password, retries=3, backoff=5):
    """Login OJS devolviendo una requests.Session autenticada."""
    s = requests.Session()
    s.verify = False
    s.headers["User-Agent"] = ("Mozilla/5.0 (X11; Linux x86_64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "descargar_enlace.py Chrome/120")
    base = f"{host}/index.php/{journal}"
    last_err = None
    for intento in range(retries):
        try:
            r = s.get(f"{base}/login", timeout=30)
            m = (re.search(r'name="csrfToken"\s+value="([^"]+)"', r.text) or
                 re.search(r'"csrfToken":"([^"]+)"', r.text))
            if not m:
                raise RuntimeError("CSRF no encontrado en /login")
            r = s.post(f"{base}/login/signIn", data={
                "csrfToken": m.group(1), "source": "",
                "username": user, "password": password, "remember": "1",
            }, timeout=30, allow_redirects=True)
            chk = s.get(f"{base}/submissions", allow_redirects=False, timeout=20)
            if chk.status_code == 302 and "/login" in chk.headers.get("Location", ""):
                raise RuntimeError("credenciales rechazadas por el servidor")
            return s
        except Exception as e:
            last_err = e
            if intento < retries - 1:
                time.sleep(backoff)
    raise RuntimeError(f"login fallido en {journal}: {last_err}")


# --- Descarga ---------------------------------------------------------------

DEFAULT_DIR = Path.home() / "Descargas"


def safe_filename(name, journal, file_id, sub_id):
    if name:
        name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip().strip(".")
        if name:
            return name
    return f"{journal}_sub{sub_id}_file{file_id}.bin"


def print_progress(written, total, bar_width=20):
    """Imprime barra de progreso simple en stderr (no interfiere con stdout)."""
    if total <= 0:
        # sin Content-Length, mostrar solo bytes
        sys.stderr.write(f"\r  {written / (1024 * 1024):6.2f} MB   ")
        sys.stderr.flush()
        return
    pct = min(written / total, 1.0)
    filled = int(pct * bar_width)
    bar = "█" * filled + "░" * (bar_width - filled)
    mb_done = written / (1024 * 1024)
    mb_total = total / (1024 * 1024)
    sys.stderr.write(f"\r  {bar} {pct*100:5.1f}%  {mb_done:6.2f}/{mb_total:.2f} MB   ")
    sys.stderr.flush()


def download_one(url, dest_dir, pedir_creds=False, quiet=False):
    """Descarga un enlace OJS. Devuelve True si OK, False si falló."""
    # 1. Detectar revista desde URL
    try:
        info = detect_from_url(url)
    except ValueError as e:
        print(c(f"  ✗ {e}", "red"))
        return False

    journal = info["journal"]
    host = info["host"]
    sub_id = info["submission_id"]
    file_id = info["file_id"]
    stage_id = info.get("stage_id", "1")
    download_url = info.get("download_url") or url

    if not quiet:
        print(c(f"  ▸ Revista detectada: ", "cyan") +
              c(journal, "yellow") + c(f"  ({host})", "gray"))
        print(c(f"    Submission: ", "gray") + str(sub_id) +
              c(f"   File: ", "gray") + str(file_id))

    # 2. Credenciales
    user = get_user()
    if pedir_creds:
        user = input(c("  Usuario OJS [riroennis]: ", "blue")).strip() or DEFAULT_USER
        pw = input(c(f"  Password para {journal}: ", "blue")).strip()
    else:
        pw = get_password_for(journal)

    if not user or not pw:
        print(c(f"  ✗ Sin credenciales para {journal}. "
                f"Define OJS_USER / OJS_PASS / OJS_PASS_REVCARD, o usa --pedir-creds.",
                "red"))
        return False

    # 3. Login
    if not quiet:
        print(c(f"  🔐 Logueando en {journal}... ", "gray"), end="", flush=True)
    try:
        s = login(host, journal, user, pw)
        if not quiet:
            print(c("OK", "green"))
    except Exception as e:
        if not quiet:
            print(c(f"FAIL: {str(e)[:120]}", "red"))
        return False

    # 4. Descargar
    if not quiet:
        print(c(f"  ⬇️  Descargando... ", "gray"))

    # Reanudar si ya existe parte del archivo
    dest_name_guess = None  # aún no sabemos el nombre real
    existing_size = 0
    headers = {}
    # Primero hacemos un HEAD para saber Content-Disposition y Content-Length
    try:
        head = s.head(download_url, timeout=30, allow_redirects=True)
        cd = head.headers.get("Content-Disposition", "")
        fname_m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
        if fname_m:
            dest_name_guess = safe_filename(fname_m.group(1), journal,
                                           file_id, sub_id)
        total = int(head.headers.get("Content-Length", 0))
    except Exception:
        total = 0

    if not dest_name_guess:
        dest_name_guess = safe_filename(None, journal, file_id, sub_id)
    dest = dest_dir / dest_name_guess
    # evitar sobreescribir: si existe, anadir .1 .2 ...
    n = 1
    while dest.exists() and dest.stat().st_size > 0:
        # intentar reanudar
        existing_size = dest.stat().st_size
        if total and existing_size >= total:
            if not quiet:
                print(c(f"  ✓ Ya existe completo: {dest.name} "
                        f"({existing_size/(1024*1024):.1f} MB)", "green"))
            return True
        # si el server soporta Range, reanudar; si no, saltar a otro nombre
        new_dest = dest_dir / f"{Path(dest_name_guess).stem}.{n}{Path(dest_name_guess).suffix}"
        n += 1
        if new_dest.exists():
            continue
        # probar reanudar en `dest`
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"
            break
        dest = new_dest

    try:
        r = s.get(download_url, stream=True, timeout=600,
                  allow_redirects=True, headers=headers)
    except Exception as e:
        print(c(f"  ✗ Red: {e}", "red"))
        return False

    if r.status_code not in (200, 206):
        print(c(f"  ✗ HTTP {r.status_code}: {r.text[:120]}", "red"))
        return False

    ctype = r.headers.get("Content-Type", "")
    if "text/html" in ctype.lower() and "application/pdf" not in ctype.lower():
        print(c(f"  ✗ El servidor devolvió HTML (no el archivo). "
                f"Probablemente: sesión caducada, rol insuficiente, o "
                f"submissionFileId erróneo.", "red"))
        return False

    # Refinar el nombre si el GET trae Content-Disposition diferente al HEAD
    cd = r.headers.get("Content-Disposition", "")
    fname_m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if fname_m:
        new_name = safe_filename(fname_m.group(1), journal, file_id, sub_id)
        if new_name != dest_name_guess:
            dest = dest_dir / new_name
            n = 1
            while dest.exists() and dest.stat().st_size > 0:
                dest = dest_dir / f"{Path(new_name).stem}.{n}{Path(new_name).suffix}"
                n += 1

    # Tamaño total (considerando reanudación)
    content_length = r.headers.get("Content-Length", "")
    try:
        cl = int(content_length)
    except (TypeError, ValueError):
        cl = 0
    total_to_download = (existing_size + cl) if existing_size else cl

    mode = "ab" if existing_size and r.status_code == 206 else "wb"
    written = existing_size if mode == "ab" else 0
    t0 = time.time()
    last_print = 0
    with open(dest, mode) as f:
        for chunk in r.iter_content(64 * 1024):
            if not chunk:
                continue
            f.write(chunk)
            written += len(chunk)
            now = time.time()
            if now - last_print > 0.5:
                last_print = now
                print_progress(written, total_to_download or total)
    sys.stderr.write("\r" + " " * 60 + "\r")  # limpiar línea
    sys.stderr.flush()

    mb = written / (1024 * 1024)
    dt = time.time() - t0
    speed = mb / dt if dt > 0 else 0
    print(c(f"  ✓ OK ", "green") +
          f"({mb:.1f} MB, {dt:.1f}s, {speed:.1f} MB/s) -> " +
          c(str(dest), "blue"))
    return True


# --- Entrada ----------------------------------------------------------------

def collect_urls(args):
    """Recolecta URLs desde args (URLs sueltas, archivos .txt, o stdin)."""
    urls = []
    if args.url_or_file:
        for arg in args.url_or_file:
            if arg == "-":
                urls.extend(sys.stdin.read().splitlines())
            elif arg.startswith("http"):
                urls.append(arg)
            else:
                # es un archivo
                try:
                    txt = Path(arg).read_text(encoding="utf-8")
                except Exception as e:
                    print(c(f"  ✗ no pude leer {arg}: {e}", "red"))
                    continue
                for line in txt.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    m = re.search(r"https?://\S+", line)
                    if m:
                        urls.append(m.group(0))
                    elif "submissionFileId" in line:
                        # quizás URL sin esquema
                        urls.append("https://" + line if not line.startswith("http") else line)
    return urls


def interactive_prompt():
    """Modo interactivo: pega enlaces, línea en blanco para terminar."""
    print(c("=" * 60, "gray"))
    print(c("  descargar_enlace.py — modo interactivo", "bold"))
    print(c("=" * 60, "gray"))
    print()
    print(c("Pega los enlaces (uno por línea).", "cyan"))
    print(c("Línea en blanco o 'fin' para terminar.", "cyan"))
    print(c("Destino: ", "gray") + str(DEFAULT_DIR))
    print()
    urls = []
    while True:
        try:
            line = input(c(f"[{len(urls)+1}] > ", "blue")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line or line.lower() in ("fin", "exit", "quit", "q"):
            break
        # si no empieza con http, asumir que es URL sin esquema
        if not line.startswith("http"):
            # intentar ver si parece URL
            if "sld.cu" in line or "submissionFileId" in line:
                line = "https://" + line
            else:
                # quizás es un archivo .txt local
                if Path(line).exists():
                    txt = Path(line).read_text(encoding="utf-8")
                    for ln in txt.splitlines():
                        ln = ln.strip()
                        if ln and not ln.startswith("#"):
                            m = re.search(r"https?://\S+", ln)
                            if m:
                                urls.append(m.group(0))
                    continue
                print(c(f"  ✗ No parece URL ni archivo: {line[:60]}", "red"))
                continue
        urls.append(line)
    return urls


def main():
    load_env()

    p = argparse.ArgumentParser(
        description=c("Descarga archivos OJS desde enlaces del bot de Telegram.",
                     "bold"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python descargar_enlace.py                                  # modo interactivo
  python descargar_enlace.py "https://revcardiologia.sld.cu/...download-file?..."
  python descargar_enlace.py enlaces.txt
  python descargar_enlace.py URL1 URL2 URL3
  cat enlaces.txt | python descargar_enlace.py -
  python descargar_enlace.py -o ~/Descargas/MisPDFs URL
  python descargar_enlace.py --pedir-creds URL
""")
    p.add_argument("url_or_file", nargs="*",
                   help="URL OJS, archivo .txt con URLs, o '-' para stdin "
                        "(si no se pasa nada, entra en modo interactivo)")
    p.add_argument("-o", "--outdir", default=str(DEFAULT_DIR),
                   help=f"directorio destino (default: {DEFAULT_DIR})")
    p.add_argument("--user", help="override usuario OJS (default: riroennis)")
    p.add_argument("--pedir-creds", action="store_true",
                   help="pedir usuario y password por prompt (no usar embebidos)")
    args = p.parse_args()

    if args.user:
        os.environ["OJS_USER"] = args.user
        os.environ["DESCARGAR_USER"] = args.user

    dest_dir = Path(args.outdir).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Decidir URLs
    if not args.url_or_file:
        urls = interactive_prompt()
    else:
        urls = collect_urls(args)

    if not urls:
        print(c("No hay URLs para descargar.", "yellow"))
        return 1

    print()
    print(c(f"🎯 {len(urls)} enlace(s) a descargar en ", "bold") +
          c(str(dest_dir), "blue"))
    print()
    ok = 0
    for u in urls:
        print(c(f"▶ Enlace {urls.index(u)+1}/{len(urls)}", "magenta"))
        if download_one(u, dest_dir, pedir_creds=args.pedir_creds):
            ok += 1
        print()
    print(c(f"=== Resumen: ", "bold") +
          c(f"{ok}/{len(urls)} OK", "green" if ok == len(urls) else "yellow"))
    return 0 if ok == len(urls) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(c("\n\nInterrumpido por usuario.", "yellow"))
        sys.exit(130)
