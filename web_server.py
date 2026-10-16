"""
web_server.py — Wrapper HTTP para el bot de Telegram en Render (tier free).

Render free Web Services se duermen tras 15 min sin requests HTTP entrantes.
Este wrapper:
  1. Arranca el bot en un thread en background (polling de Telegram).
  2. Expone un servidor HTTP mínimo en el PORT que Render asigna.
  3. Responde 200 OK a / y /health (para que UptimeRobot/cron-job.org
     pueda hacer ping cada 10 min y evitar el sleep).

USO:
  - Si API_ID y API_HASH están definidos → usa bot_telegram.py (Pyrogram,
    soporta archivos grandes vía MTProto).
  - Si NO están definidos → usa bot_telegram_simple.py (requests, límite 20MB).

Arranque local:
  PORT=10000 python web_server.py
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Decidir qué bot usar según disponibilidad de API_ID/API_HASH
USE_PYROGRAM = bool(os.environ.get("API_ID") and os.environ.get("API_HASH"))

if USE_PYROGRAM:
    import bot_telegram as bot  # noqa: E402
else:
    import bot_telegram_simple as bot  # noqa: E402

BOT_THREAD = None
BOT_ERROR = None


def start_bot_thread():
    """Arranca el polling del bot en un thread daemon."""
    global BOT_THREAD, BOT_ERROR

    def _run():
        global BOT_ERROR
        try:
            if USE_PYROGRAM:
                # Pyrogram es async: usar asyncio.run
                import asyncio
                asyncio.run(bot.main_async())
            else:
                # bot_simple es síncrono
                bot.main()
        except Exception as e:
            BOT_ERROR = str(e)
            print(f"[BOT] error fatal: {e}", file=sys.stderr)

    BOT_THREAD = threading.Thread(target=_run, daemon=True, name="bot")
    BOT_THREAD.start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # silenciar logs de access (Render ya loggea)
        pass

    def do_GET(self):
        if self.path in ("/", "/health", "/ping"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.path == "/health":
                body = b"OK\n"
            else:
                alive = "running" if (BOT_THREAD and BOT_THREAD.is_alive()) \
                    else f"stopped ({BOT_ERROR or '—'})"
                mode = "Pyrogram (MTProto, sin límite)" if USE_PYROGRAM \
                    else "requests (límite 20MB)"
                body = (
                    f"Bot Telegram OJS\n"
                    f"Status: {alive}\n"
                    f"Mode: {mode}\n"
                    f"Bot: @Uptimeriroobot\n"
                    f"Revistas: revcardiologia, mil, rme, cocmed, ems, spu\n"
                    f"\nPing /health para health-check (200 OK).\n"
                ).encode()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found\n")


def main():
    print(f"[INIT] Modo: {'Pyrogram' if USE_PYROGRAM else 'requests'}",
          flush=True)
    print("[INIT] Arrancando bot en background...", flush=True)
    start_bot_thread()
    print(f"[INIT] Bot thread: alive={BOT_THREAD.is_alive()}", flush=True)

    port = int(os.environ.get("PORT", "10000"))
    host = "0.0.0.0"
    server = HTTPServer((host, port), Handler)
    print(f"[INIT] HTTP server escuchando en http://{host}:{port}", flush=True)
    print(f"[INIT] Health-check: http://{host}:{port}/health", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INIT] Apagando...", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()

