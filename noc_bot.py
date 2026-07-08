"""
NOC Bot - Monitoreo y comandos remotos via Telegram
====================================================
Version preparada para Railway (usa variables de entorno / .env)

Requisitos (ver requirements.txt):
    python-telegram-bot==21.*
    asyncssh
    python-dotenv
"""

import os
import re
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

import asyncssh
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

load_dotenv()  # en local lee el archivo .env; en Railway las variables ya están inyectadas

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("noc_bot")

# ------------------------------------------------------------------
# CONFIGURACIÓN — TODO VIENE DE VARIABLES DE ENTORNO
# ------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "30"))


@dataclass
class ServerConfig:
    name: str
    host: str
    port: int
    user: str
    password: str  # autenticación por password (no llave SSH)


def _load_server(prefix: str) -> ServerConfig | None:
    """Carga la config de un servidor a partir de variables SERVER1_*, SERVER2_*, etc.
    Si faltan variables obligatorias (NAME, HOST, USER, PASSWORD), devuelve None
    en vez de tumbar el bot, para poder arrancar aunque falte un servidor."""
    required = [f"{prefix}_NAME", f"{prefix}_HOST", f"{prefix}_USER", f"{prefix}_PASSWORD"]
    if not all(os.getenv(k) for k in required):
        log.warning(f"{prefix} no está configurado (faltan variables) — se omite.")
        return None
    return ServerConfig(
        name=os.environ[f"{prefix}_NAME"],
        host=os.environ[f"{prefix}_HOST"],
        port=int(os.getenv(f"{prefix}_PORT", "22")),
        user=os.environ[f"{prefix}_USER"],
        password=os.environ[f"{prefix}_PASSWORD"],
    )


SERVERS = [s for s in (_load_server("SERVER1"), _load_server("SERVER2")) if s is not None]

if not SERVERS:
    log.warning("Ningún servidor configurado. El bot arrancará pero no podrá reportar nada.")

SERVICES_TO_WATCH = {
    "apache2": os.getenv("SERVICE_APACHE", "apache2"),
    "mysql": os.getenv("SERVICE_MYSQL", "mysql"),
    "ftp": os.getenv("SERVICE_FTP", "vsftpd"),
}

# ------------------------------------------------------------------
# ESTADO GLOBAL: notificaciones on/off + historial de errores
# ------------------------------------------------------------------

_notifications_enabled = True

# Cada entrada: {"time": datetime, "server": str, "host": str, "service": str, "status": str}
_error_history: list[dict] = []
MAX_HISTORY = 200  # tope para no crecer infinito en memoria

# ------------------------------------------------------------------
# CONEXIÓN SSH Y EJECUCIÓN DE COMANDOS
# ------------------------------------------------------------------

async def run_remote(server: ServerConfig, command: str) -> str:
    """Ejecuta un comando remoto por SSH (password) y devuelve stdout (o error)."""
    try:
        async with asyncssh.connect(
            server.host,
            port=server.port,
            username=server.user,
            password=server.password,
            known_hosts=None,
            # root con password suele requerir habilitar keyboard-interactive/password explícito
            preferred_auth=["password", "keyboard-interactive"],
        ) as conn:
            result = await conn.run(command, check=False, timeout=10)
            out = (result.stdout or "").strip()
            err = (result.stderr or "").strip()
            return out if out else (err or "(sin salida)")
    except Exception as e:
        return f"⚠️ Error de conexión: {e}"


async def run_on_all(command: str) -> dict:
    tasks = [run_remote(s, command) for s in SERVERS]
    results = await asyncio.gather(*tasks)
    return dict(zip([s.name for s in SERVERS], results))


def format_multi(title: str, results: dict) -> str:
    lines = [f"📋 *{title}*", ""]
    for server in SERVERS:
        out = results.get(server.name, "(sin datos)")
        lines.append(f"🖥️ *{server.name}* ({server.host})")
        lines.append(f"```\n{out}\n```")
    if len(SERVERS) < 2:
        lines.append("⚠️ _Solo hay 1 servidor configurado. Falta agregar el segundo (SERVER2_*) en las variables de entorno._")
    return "\n".join(lines)


# ------------------------------------------------------------------
# COMANDOS DE ESTADO DE SERVICIO
# ------------------------------------------------------------------

async def cmd_estado_servicio(update: Update, context: ContextTypes.DEFAULT_TYPE, service_name: str):
    results = await run_on_all(f"systemctl is-active {service_name} 2>/dev/null || echo inactive")
    formatted = {}
    for name, status in results.items():
        emoji = "✅" if status.strip() == "active" else "❌"
        formatted[name] = f"{emoji} {status.strip()}"
    text = format_multi(f"Estado de {service_name}", formatted)
    await update.message.reply_text(text, parse_mode="Markdown")


# ------------------------------------------------------------------
# COMANDOS DE RECURSOS (resumidos)
# ------------------------------------------------------------------

async def cmd_ver_espacio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = "df -h / | awk 'NR==2{printf \"Usado: %s / %s (%s)\\nDisponible: %s\", $3,$2,$5,$4}'"
    results = await run_on_all(cmd)
    await update.message.reply_text(format_multi("Espacio en disco", results), parse_mode="Markdown")


async def cmd_ver_memoria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = "free -h | awk 'NR==2{printf \"Usada: %s / %s\\nLibre: %s\", $3,$2,$4}'"
    results = await run_on_all(cmd)
    await update.message.reply_text(format_multi("Uso de memoria RAM", results), parse_mode="Markdown")


async def cmd_ver_usuario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = "who | awk '{print $1\" - \"$2\" - \"$3\" \"$4}' || echo 'Sin usuarios conectados'"
    results = await run_on_all(cmd)
    await update.message.reply_text(format_multi("Usuarios conectados", results), parse_mode="Markdown")


async def cmd_ver_cpu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = "top -bn1 | grep 'Cpu(s)' | awk '{printf \"Uso CPU: %.1f%%\", 100-$8}'"
    results = await run_on_all(cmd)
    await update.message.reply_text(format_multi("Uso de CPU", results), parse_mode="Markdown")


# ------------------------------------------------------------------
# TEXTO DE AYUDA / LISTA DE COMANDOS
# ------------------------------------------------------------------

def help_text(mention: str = "") -> str:
    saludo = f"👋 ¡Bienvenido/a {mention}!\n\n" if mention else ""
    estado_notif = "🔔 activadas" if _notifications_enabled else "🔕 desactivadas"
    return (
        f"{saludo}"
        "🤖 *Bot NOC — Monitoreo y comandos remotos*\n\n"
        "Puedes escribir estos comandos tal cual, en minúsculas:\n\n"
        "*Estado de servicios:*\n"
        "• `estado servicio apache2`\n"
        "• `estado servicio mysql`\n"
        "• `estado servicio ftp`\n\n"
        "*Recursos del servidor:*\n"
        "• `ver espacio` — espacio en disco\n"
        "• `ver memoria` — uso de RAM\n"
        "• `ver usuario` — usuarios conectados\n"
        "• `ver cpu` — uso de CPU\n\n"
        "*Alertas automáticas:*\n"
        "• `notifications off` — silencia las alertas de caídas\n"
        "• `notifications on` — vuelve a activarlas\n"
        f"• Estado actual: {estado_notif}\n\n"
        "*Historial:*\n"
        "• `history error N` — últimos N errores registrados (del más reciente al más antiguo)\n\n"
        "Todas las respuestas incluyen datos de *ambos* servidores.\n\n"
        "Escribe `info` en cualquier momento para volver a ver esta lista."
    )


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text(), parse_mode="Markdown")


async def on_new_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Se dispara cuando alguien nuevo entra al grupo (o el bot mismo es agregado)."""
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue  # no saludar si el que entra es otro bot
        mention = member.first_name or "nuevo miembro"
        await update.message.reply_text(help_text(mention=mention), parse_mode="Markdown")


async def cmd_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE, turn_on: bool):
    global _notifications_enabled
    _notifications_enabled = turn_on
    if turn_on:
        await update.message.reply_text("🔔 Notificaciones de caídas de servicio *activadas*.", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "🔕 Notificaciones de caídas de servicio *desactivadas*.\n"
            "El bot sigue monitoreando y guardando el historial, solo no va a mandar avisos hasta que actives con `notifications on`.",
            parse_mode="Markdown",
        )


async def cmd_history_error(update: Update, context: ContextTypes.DEFAULT_TYPE, n: int):
    if n <= 0:
        await update.message.reply_text("Pon un número mayor a 0, ej: `history error 3`", parse_mode="Markdown")
        return

    if not _error_history:
        await update.message.reply_text("📭 Todavía no hay errores registrados.")
        return

    # del más reciente al más antiguo
    ultimos = list(reversed(_error_history))[:n]

    lines = [f"📜 *Últimos {len(ultimos)} error(es) registrados:*", ""]
    for i, e in enumerate(ultimos, start=1):
        ts = e["time"].strftime("%d/%m/%Y %H:%M:%S")
        lines.append(
            f"{i}. 🚨 *{e['server']}* ({e['host']}) — `{e['service']}` → `{e['status']}`\n   🕒 {ts}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")



    text = (update.message.text or "").strip().lower()

    if text == "estado servicio apache2":
        await cmd_estado_servicio(update, context, SERVICES_TO_WATCH["apache2"])
    elif text == "estado servicio mysql":
        await cmd_estado_servicio(update, context, SERVICES_TO_WATCH["mysql"])
    elif text == "estado servicio ftp":
        await cmd_estado_servicio(update, context, SERVICES_TO_WATCH["ftp"])
    elif text == "ver espacio":
        await cmd_ver_espacio(update, context)
    elif text == "ver memoria":
        await cmd_ver_memoria(update, context)
    elif text == "ver usuario":
        await cmd_ver_usuario(update, context)
    elif text == "ver cpu":
        await cmd_ver_cpu(update, context)
    elif text == "info":
        await cmd_info(update, context)
    elif text == "notifications off":
        await cmd_notifications(update, context, turn_on=False)
    elif text == "notifications on":
        await cmd_notifications(update, context, turn_on=True)
    elif re.fullmatch(r"history error \d+", text):
        n = int(text.split()[-1])
        await cmd_history_error(update, context, n)


async def cmd_getid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat ID: `{update.effective_chat.id}`", parse_mode="Markdown")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Captura cualquier error no previsto en un comando/mensaje.
    Así, si algo falla procesando UN mensaje, el bot sigue vivo y
    puede seguir respondiendo a los siguientes mensajes normalmente."""
    log.error(f"Error no manejado: {context.error}")


# ------------------------------------------------------------------
# MONITOREO AUTOMÁTICO DE CAÍDAS
# ------------------------------------------------------------------

_last_state = {}

async def monitor_loop(app: Application):
    global _last_state
    await asyncio.sleep(5)
    while True:
        try:
            for service_key, service_name in SERVICES_TO_WATCH.items():
                results = await run_on_all(f"systemctl is-active {service_name} 2>/dev/null || echo inactive")
                for server in SERVERS:
                    key = (server.name, service_name)
                    current = results.get(server.name, "unknown").strip()
                    previous = _last_state.get(key)

                    if previous is not None and previous == "active" and current != "active":
                        # El historial se guarda SIEMPRE, esté prendida o apagada la notificación
                        _error_history.append({
                            "time": datetime.now(),
                            "server": server.name,
                            "host": server.host,
                            "service": service_name,
                            "status": current,
                        })
                        if len(_error_history) > MAX_HISTORY:
                            del _error_history[0]

                        if _notifications_enabled:
                            msg = (
                                f"🚨 *ALERTA: servicio caído*\n"
                                f"🖥️ Servidor: *{server.name}* ({server.host})\n"
                                f"⚙️ Servicio: `{service_name}`\n"
                                f"Estado actual: `{current}`"
                            )
                            try:
                                await app.bot.send_message(chat_id=GROUP_CHAT_ID, text=msg, parse_mode="Markdown")
                            except Exception as e:
                                log.error(f"No se pudo enviar alerta: {e}")

                    if previous is not None and previous != "active" and current == "active":
                        if _notifications_enabled:
                            msg = (
                                f"✅ *Servicio recuperado*\n"
                                f"🖥️ Servidor: *{server.name}* ({server.host})\n"
                                f"⚙️ Servicio: `{service_name}` está `active` de nuevo"
                            )
                            try:
                                await app.bot.send_message(chat_id=GROUP_CHAT_ID, text=msg, parse_mode="Markdown")
                            except Exception as e:
                                log.error(f"No se pudo enviar alerta: {e}")

                    _last_state[key] = current
        except Exception as e:
            # Protección general: pase lo que pase en una vuelta del ciclo
            # (servidor borrado, timeout raro, error de formato, etc.),
            # el monitoreo NUNCA debe detenerse por completo.
            log.error(f"Error en monitor_loop (se ignora y se reintenta): {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def post_init(app: Application):
    asyncio.create_task(monitor_loop(app))


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("getid", cmd_getid))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_chat_member))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_error_handler(error_handler)

    log.info("Bot iniciado. Esperando mensajes...")
    app.run_polling()


if __name__ == "__main__":
    main()