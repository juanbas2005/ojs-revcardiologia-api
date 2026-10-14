"""
Prueba de límite de MB por archivo en cada revista OJS cubana.

Genera archivos de tamaño creciente, los sube a una submission por revista
y detiene la prueba en el primer fallo. Reporta el mayor tamaño que se
subió bien y el primero que falló.

Credenciales vienen de variables de entorno (GitHub Secrets), nunca del repo.
"""
import os
import re
import json
import sys
import time

import requests
requests.packages.urllib3.disable_warnings()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, ROOT)
from ojs_api import OJSApi  # noqa: E402

USERNAME = os.environ["OJS_USER"]
PASS_DEFAULT = os.environ["OJS_PASS"]      # mil, rme, cocmed, ems
PASS_REVCARD = os.environ["OJS_PASS_REVCARD"]  # revcardiologia

# Sondeo geográfico; luego búsqueda binaria entre último OK y primer fallo.
PROBES_MB = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]
MAX_MB = 512
GRANULARITY_MB = 2  # precisión del límite reportado


def make_file(path, mb):
    with open(path, "wb") as f:
        f.truncate(mb * 1024 * 1024)


def upload(api, sub, path, mb):
    """Intenta subir; devuelve (ok, detalle)."""
    try:
        with open(path, "rb") as f:
            r = api.session.post(
                f"{api.base}/api/v1/submissions/{sub}/files",
                headers={"X-CSRF-TOKEN": api.csrf},
                files={"file": (f"prueba_{mb}MB.pdf", f,
                                "application/pdf")},
                data={"fileStage": api.file_stage, "genreId": api.genre_id},
                timeout=900)
    except Exception as e:
        return False, f"excepción: {e}"
    if r.status_code == 200:
        try:
            return True, None if r.json().get("id") else f"sin id: {r.text[:100]}"
        except Exception:
            return False, f"no json: {r.text[:100]}"
    return False, f"HTTP {r.status_code}: {r.text[:120]}"


def find_limit(host, journal, password):
    api = OJSApi(host, USERNAME, password, journal=journal)
    # el DNS de .sld.cu a veces falla transitoriamente: reintentar
    ok = False
    for _ in range(3):
        try:
            api.login()
            ok = True
            break
        except Exception as e:
            print(f"  {journal}: login reintentando ({e})")
            time.sleep(15)
    if not ok:
        try:
            api.login()
        except Exception as e:
            return {"revista": journal, "estado": f"login falló: {e}"}
    try:
        sub = int(api.create())
    except Exception as e:
        return {"revista": journal, "estado": f"crear submission falló: {e}"}

    tmp = os.path.join("/tmp", f"ojs_{journal}.bin")
    last_ok = first_fail = None

    for mb in PROBES_MB:
        make_file(tmp, mb)
        ok, det = upload(api, sub, tmp, mb)
        print(f"  {journal}: {mb} MB -> {'OK' if ok else det}")
        if ok:
            last_ok = mb
        else:
            first_fail = mb
            break

    # si todo el sondeo pasó hasta 512, no conocemos límite superior
    if last_ok is None:
        return {"revista": journal, "estado": "ni 1 MB sube", "detail": first_fail}

    # búsqueda binaria entre last_ok y first_fail
    if first_fail is None:
        return {"revista": journal, "límite_MB": f">= {MAX_MB} (no falló en la prueba)",
                "máximo_ok_MB": last_ok, "primer_fallo_MB": None}
    lo, hi = last_ok, first_fail
    while hi - lo > GRANULARITY_MB:
        mid = (lo + hi) // 2
        make_file(tmp, mid)
        ok, det = upload(api, sub, tmp, mid)
        print(f"  {journal}: refine {mid} MB -> {'OK' if ok else det}")
        if ok:
            lo = mid
        else:
            hi = mid
    return {"revista": journal, "límite_MB": f"entre {lo} y {hi}",
            "máximo_ok_MB": lo, "primer_fallo_MB": hi}


def main():
    with open(os.path.join(ROOT, "revistas.json"), encoding="utf-8") as f:
        revistas = json.load(f)["revistas"]
    resultados = []
    for r in revistas:
        if not r.get("compatible") or r["journal"] == "spu":
            continue
        print(f"▶ {r['journal']} ({r['nombre']})")
        pw = PASS_REVCARD if r["journal"] == "revcardiologia" else PASS_DEFAULT
        res = find_limit(r["host"], r["journal"], pw)
        resultados.append(res)
        print("  ", json.dumps(res, ensure_ascii=False))

    salida = json.dumps({"resultados": resultados}, ensure_ascii=False, indent=2)
    print("\n=== RESULTADO FINAL ===\n" + salida)
    with open(os.path.join(ROOT, "limite_resultado.json"), "w", encoding="utf-8") as f:
        f.write(salida)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("## Límite de MB por archivo por revista\n\n| Revista | Límite | Máx OK | 1er fallo |\n|---|---|---|---|\n")
            for x in resultados:
                f.write(f"| {x.get('revista')} | {x.get('límite_MB','—')} | "
                        f"{x.get('máximo_ok_MB','—')} | {x.get('primer_fallo_MB','—')} |\n")


if __name__ == "__main__":
    main()
