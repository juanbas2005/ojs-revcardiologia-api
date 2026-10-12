# OJS Revcardiologia — API de subida de archivos

API en Python para subir archivos a la revista **Revista Cubana de Cardiología
y Cirugía Cardiovascular** (OJS 3.3) ubicada en:

```
https://revcardiologia.sld.cu/index.php/revcardiologia
```

Reimplementa el flujo del wizard de OJS + su REST API:
**login con CSRF → crear submission → POST multipart a
`/api/v1/submissions/{id}/files` → enlace de descarga.**

## Requisitos

- Python 3.8+
- `pip install -r requirements.txt`

## Uso (CLI)

```bash
python ojs_api.py \
  --user TU_USUARIO \
  --password TU_CLAVE \
  --file ruta/al/articulo.pdf \
  --submission 3555        # opcional; si no, se crea una nueva
```

### Salida

```json
{
  "submissionId": 3555,
  "id": 182058,
  "fileId": 18449,
  "name": {"es_ES": "articulo.pdf"},
  "url": "https://revcardiologia.sld.cu/index.php/revcardiologia/$$$call$$$/api/file/file-api/download-file?..."
}
```

## Uso (como módulo)

```python
from ojs_api import OJSApi

api = OJSApi(
    host="https://revcardiologia.sld.cu",
    username="TU_USUARIO",
    password="TU_CLAVE",
)
api.login()
submission_id = api.create(section_id="25")    # opcional
sub = api.get_submission_id()                  # o usa el que quieras
res = api.upload(3555, "articulo.pdf")
print(api.download_url(3555, res["id"]))
```

## Cómo funciona internamente

| Paso | Endpoint | Método | Detalle |
|------|----------|--------|---------|
| Login | `/login` + `/login/signIn` | GET/POST | extrae `csrfToken` del formulario |
| CSRF | `/submissions` | GET | token en `"csrfToken":"..."` del JS |
| Crear submission | `/submission/saveStep/1` | POST | checklists + `sectionId` + `userGroupId` |
| Subir archivo | `/api/v1/submissions/{id}/files` | **POST** | multipart `file` + `fileStage=2` + `genreId` |
| Enlace | `$$$call$$$/api/file/file-api/download-file` | GET | `submissionFileId`, `submissionId`, `stageId` |

### Puntos clave (por experiencia):

- Cabecera **`X-CSRF-TOKEN`** imprescindible en las llamadas a la API.
  Sin ella, OJS devuelve `403 form.csrfInvalid`.
- El campo **debe llamarse `file`** y el método es **POST** (no PUT).
- `fileStage=2` (Submission) es obligatorio; sin él → `api.submissionFiles.400.noFileStageId`.
- Extensiones permitidas: `epub xml xls cvs doc docx pdf jpg png html gif xlsx xls`.
  `.txt` NO está permitida; usa `.doc`, `.docx`, `.pdf` o `.html`.
- `genreId=1` = Texto del artículo; `12` = Otro.
- El enlace contiene `$$$call$$$`, por lo que exige **sesión iniciada**
  (no es público). Comparte la URL con quien tenga acceso a la submission,
  o pedid al gestor que la publique.

## Licencia

MIT — ver [LICENSE](LICENSE).
