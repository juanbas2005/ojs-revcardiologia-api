#!/usr/bin/env python3
"""
descargar_enlace.py — Descarga archivos de OJS a partir de los enlaces que
genera el bot de Telegram.

Acepta:
  - Un archivo .txt con URLs (una por línea; se ignoran las que empiecen con #).
  - Una o varias URLs sueltas en la línea de comandos.
  - Una URL individual leída de stdin.

Cada URL tiene la forma:
  https://<host>/index.php/<journal>/$$$call$$$/api/file/file-api/download-file
      ?submissionFileId=...&submissionId=...&stageId=...

El script:
  1. Detecta el `journal` y el `host` desde la URL.
  2. Lee las credenciales desde:
       - Variables de entorno: OJS_USER, OJS_PASS, OJS_PASS_REVCARD
       - O desde .secrets/.env (si existe, en el mismo directorio del script).
       - O desde config.json (tambien ignorado por git).
  3. Hace login automatico (con reintentos).
  4. Descarga el archivo a ~/Descargas (lo crea si no existe).
  5. Si el nombre no viene en Content-Disposition, usa:
       <journal>_sub<id>_file<fileId>.bin
  6. Resume: imprime por pantalla que descargo cada uno y donde.

Uso:
  python descargar_enlace.py enlaces.txt
  python descargar_enlace.py "https://revcardiologia.sld.cu/...download-file?..."
  cat enlaces.txt | python descargar_enlace.py -
  python descargar_enlace.py --help

Dependencias: solo `requests` (incluida en requirements.txt).
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
    sys.exit("Falta instalar requests: pip install requests")

HERE = Path(__file__).resolve().parent

# --- Carga de credenciales ---------------------------------------------------

def load_env():
    """Carga variables de .secrets/.env y/o config.json si existen."""
    # 1) .secrets/.env
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
    # 2) config.json (opcional, json plano)
    cfg_file = HERE / "config.json"
    if cfg_file.exists():
        import json
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            for k, v in cfg.items():
                os.environ.setdefault(k, str(v))
        except Exception:
            pass


def get_pass_for(journal):
    """Devuelve el password para la revista dada."""
    if journal == "revcardiologia":
        return os.environ.get("OJS_PASS_REVCARD")
    return os.environ.get("OJS_PASS") or os.environ.get("OJS_PASS_DEFAULT")


# --- Login OJS (minimo cliente, sin depender de ojs_api) --------------------

_URL_RE = re.compile(
    r"https?://([^/]+)/index\.php/([^/]+)/\$\$\$call\$\$\$/.+download-file"
    r"\?([^#\s]+)")


def parse_url(url):
    """Devuelve (host_base, journal, params_dict) o None si no es URL OJS."""
    m = _URL_RE.search(url)
    if not m:
        return None
    host_full = "https://" + m.group(1)
    journal = m.group(2)
    qs = up.parse_qs(m.group(3))
    return host_full, journal, qs


def login(host, journal, user, password, retries=3, backoff=5):
    """Login OJS devolviendo una requests.Session autenticada."""
    s = requests.Session()
    s.verify = False
    s.headers["User-Agent"] = "Mozilla/5.0 (descargar_enlace.py)"
    base = f"{host}/index.php/{journal}"
    last_err = None
    for _ in range(retries):
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
            # verificar sesion con GET a /submissions
            chk = s.get(f"{base}/submissions", allow_redirects=False, timeout=20)
            if chk.status_code == 302 and "/login" in chk.headers.get("Location", ""):
                raise RuntimeError("credenciales rechazadas")
            return s
        except Exception as e:
            last_err = e
            time.sleep(backoff)
    raise RuntimeError(f"login fallido en {journal}: {last_err}")


# --- Descarga ---------------------------------------------------------------

DEFAULT_DIR = Path.home() / "Descargas"


def safe_filename(name, journal, file_id, sub_id):
    """Sanea el nombre de archivo."""
    if name:
        name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip().strip(".")
        if name:
            return name
    return f"{journal}_sub{sub_id}_file{file_id}.bin"


def download_one(url, dest_dir):
    parsed = parse_url(url)
    if not parsed:
        print(f"  ✗ URL no reconocida (no es enlace OJS download-file): {url[:80]}")
        return False
    host, journal, qs = parsed
    sub_id = qs.get("submissionId", ["?"])[0]
    file_id = qs.get("submissionFileId", ["?"])[0]
    stage_id = qs.get("stageId", ["1"])[0]

    user = os.environ.get("OJS_USER")
    pw = get_pass_for(journal)
    if not user or not pw:
        print(f"  ✗ Sin credenciales para {journal} "
              f"(define OJS_USER / OJS_PASS / OJS_PASS_REVCARD)")
        return False

    print(f"  → {journal} sub={sub_id} file={file_id} ... ", end="", flush=True)
    try:
        s = login(host, journal, user, pw)
    except Exception as e:
        print(f"LOGIN FAIL: {str(e)[:100]}")
        return False

    try:
        r = s.get(url, stream=True, timeout=300, allow_redirects=True)
    except Exception as e:
        print(f"RED FAIL: {e}")
        return False
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:120]}")
        return False
    ctype = r.headers.get("Content-Type", "")
    if "text/html" in ctype.lower():
        print("no autorizado (HTML inesperado — revisa rol/credenciales)")
        return False

    # Nombre desde Content-Disposition
    cd = r.headers.get("Content-Disposition", "")
    fname_m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    fname = safe_filename(fname_m.group(1) if fname_m else None,
                          journal, file_id, sub_id)
    dest = dest_dir / fname
    # evitar sobreescribir: si existe, anadir .1 .2 ...
    n = 1
    while dest.exists():
        dest = dest_dir / f"{Path(fname).stem}.{n}{Path(fname).suffix}"
        n += 1

    total = int(r.headers.get("Content-Length", 0))
    written = 0
    t0 = time.time()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(64 * 1024):
            if chunk:
                f.write(chunk)
                written += len(chunk)
    mb = written / (1024 * 1024)
    dt = time.time() - t0
    speed = mb / dt if dt > 0 else 0
    print(f"OK ({mb:.1f} MB, {dt:.1f}s, {speed:.1f} MB/s) -> {dest}")
    return True


# --- Entrada ----------------------------------------------------------------

def collect_urls(args):
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
                    print(f"  ✗ no pude leer {arg}: {e}")
                    continue
                for line in txt.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # extraer la primera URL de la línea (por si hay texto extra)
                    m = re.search(r"https?://\S+", line)
                    if m:
                        urls.append(m.group(0))
    return urls


def main():
    load_env()
    p = argparse.ArgumentParser(
        description="Descarga archivos OJS desde enlaces del bot de Telegram.")
    p.add_argument("url_or_file", nargs="+",
                   help="URL OJS, archivo .txt con URLs, o '-' para stdin")
    p.add_argument("-o", "--outdir", default=str(DEFAULT_DIR),
                   help=f"directorio destino (default: {DEFAULT_DIR})")
    p.add_argument("--user", help="override OJS_USER")
    args = p.parse_args()
    if args.user:
        os.environ["OJS_USER"] = args.user

    dest_dir = Path(args.outdir).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)

    urls = collect_urls(args)
    if not urls:
        print("No hay URLs para descargar.")
        return 1
    print(f"Descargando {len(urls)} archivo(s) en {dest_dir}\n")
    ok = 0
    for u in urls:
        if download_one(u, dest_dir):
            ok += 1
    print(f"\n=== Resumen: {ok}/{len(urls)} OK ===")
    return 0 if ok == len(urls) else 1


if __name__ == "__main__":
    sys.exit(main())
