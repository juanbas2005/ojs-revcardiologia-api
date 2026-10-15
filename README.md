# OJS Revcardiologia — API + Bot Telegram para revistas cubanas

Sistema para subir archivos a revistas cubanas OJS 3.3 y descargarlos,
con un bot de Telegram que automatiza el flujo.

## Estructura

```
.
├── ojs_api.py               # Núcleo: login + subida + descarga OJS
├── bot_telegram.py          # Bot con Pyrogram (requiere API_ID/API_HASH)
├── bot_telegram_simple.py   # Bot con solo requests (sin api_id/hash) — DEFAULT
├── descargar_enlace.py      # Script standalone para descargar enlaces OJS
├── revistas.json            # Registro de revistas cubanas + límites MB
├── requirements.txt         # Dependencias Python
├── tests/
│   └── limite_archivo.py    # Prueba de límite de MB por revista
├── .github/workflows/
│   ├── bot_keepalive.yml    # Workflow principal: bot + cron + health check
│   ├── bot_telegram.yml     # Workflow legacy (dispatch manual)
│   └── limite_subida.yml    # Probar límites (dispatch manual)
├── GUIA.md                  # Guía completa de uso
└── README.md                # Este archivo
```

## Revistas soportadas (OJS 3.3.0.18)

| Slug | Revista | Host | Límite MB |
|------|---------|------|-----------|
| `revcardiologia` | Rev. Cubana de Cardiología | revcardiologia.sld.cu | 19 |
| `mil` | Rev. Cubana de Medicina Militar | revmedmilitar.sld.cu | 19 |
| `rme` | Rev. Electrónica Zoilo Marinello | revmedicaelectronica.sld.cu | 19 |
| `cocmed` | Rev. Cubana Medicina General Integral | revcocmed.sld.cu | 19 |
| `ems` | Rev. Cubana Educación Médica Superior | ems.sld.cu | 19 |
| `spu` | Rev. Cubana Salud Pública | revsaludpublica.sld.cu | 19* |
| `medicentro` | Medicentro | medicentro.sld.cu | ❌ OJS 2.x |

(*) SPU requiere verificación de credenciales — ver `GUIA.md` §10.

## Inicio rápido

1. **Clona** el repo y entra:
   ```bash
   git clone https://github.com/juanbas2005/ojs-revcardiologia-api.git
   cd ojs-revcardiologia-api
   ```

2. **Instala** dependencias:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configura** credenciales en `.secrets/.env`:
   ```bash
   mkdir -p .secrets
   cat > .secrets/.env <<'EOF'
   TELEGRAM_BOT_TOKEN=tu-token
   OJS_USER=riroennis
   OJS_PASS=Riroennis@2026
   OJS_PASS_REVCARD=Pupi@#1212
   EOF
   chmod 600 .secrets/.env
   ```

4. **Arranca el bot**:
   ```bash
   python bot_telegram_simple.py
   ```

5. **Abre el bot** en Telegram (bot @Uptimeriroobot o el tuyo propio) y
   envía `/start`.

Para más detalles, **lee [`GUIA.md`](GUIA.md)**.

## Cómo funciona

### Subida a OJS (cliente `ojs_api.py`)

| Paso | Endpoint | Método | Detalle |
|------|----------|--------|---------|
| Login | `/login` + `/login/signIn` | GET/POST | extrae `csrfToken` del formulario |
| Verificar sesión | `/submissions` | GET | no debe redirigir a `/login` |
| CSRF | `/submissions` o `/dashboard` | GET | token en JSON o en `value=` HTML |
| Crear submission | `/submission/saveStep/1` | POST | checklists + `sectionId` + `userGroupId` |
| Subir archivo | `/api/v1/submissions/{id}/files` | POST | multipart `file` + `fileStage=2` + `genreId=1` |
| Enlace | `$$$call$$$/api/file/file-api/download-file` | GET | requiere sesión iniciada |
| Descargar | el enlace anterior | GET stream | `Content-Type` debe ser binario, no HTML |

### Bot (polling con `requests` o Pyrogram)

- Recibe archivo/video/enlace del chat.
- Descarga a `/tmp`.
- Si extensión no permitida → renombra a `.pdf`.
- Parte en trozos de `PART_MB` MB (5 por defecto, máx 19).
- Sube cada parte a OJS (con barra de progreso).
- Genera `.txt` con enlaces y lo envía al chat.

## Límites técnicos confirmados

- **Límite de MB por archivo**: 19 MB en todas las revistas OJS 3.3
  cubanas (falla en 20 MB con `noFileStageId` — síntoma de
  `post_max_size` PHP excedido).
- **GitHub Actions**: 2000 min/mes, 6h por job. Usamos cron de 30 min
  + idle de 30 min → ~1 job/hora. Recomendado VPS para 24/7 real.
- **Pyrogram 2.x** requiere `api_id` + `api_hash` (gratis en
  <https://my.telegram.org/apps>).

## Licencia

MIT — ver [LICENSE](LICENSE).
