import os
import openai
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
import dateparser
import re

# --- Cargar variables de entorno ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

# --- Inicializa Firebase ---
if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# --- Estructura de campos del recordatorio ---
CAMPOS = [
    "cliente", "num_cliente", "proyecto", "modalidad", "fecha_hora", "motivo", "observaciones"
]

# --- Estados de usuario para flujo conversacional ---
user_states = {}

def extraer_datos(texto):
    """Extrae los datos del recordatorio usando OpenAI."""
    prompt = f"""
Extrae estos campos del siguiente mensaje: {CAMPOS}.
Responde SOLO un JSON válido. Si falta un campo, déjalo vacío.

Ejemplo:
Mensaje: Agenda cita con Juan Carlos el lunes a las 8pm.
Respuesta: {{
  "cliente": "Juan Carlos",
  "num_cliente": "",
  "proyecto": "",
  "modalidad": "",
  "fecha_hora": "próximo lunes 8pm",
  "motivo": "cita",
  "observaciones": ""
}}

Mensaje: {texto}
Respuesta:
    """
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0
    )
    import json
    text = response.choices[0].message.content
    try:
        # Asegura que sea solo el JSON
        match = re.search(r'\{[\s\S]+\}', text)
        if match:
            return json.loads(match.group(0))
        else:
            return {k: "" for k in CAMPOS}
    except Exception:
        return {k: "" for k in CAMPOS}

def parse_fecha_hora(fecha_str):
    """Convierte un string a datetime usando dateparser."""
    if not fecha_str:
        return None
    dt = dateparser.parse(fecha_str, languages=['es'])
    if dt:
        if dt.tzinfo is None:
            tz = pytz.timezone("America/Lima")
            dt = tz.localize(dt)
        return dt
    return None

async def pedir_datos_faltantes(context, chat_id, datos):
    """Pregunta por los campos faltantes."""
    faltantes = [campo for campo, valor in datos.items() if not valor]
    if not faltantes:
        return False
    msg = "Por favor, indícame: " + ", ".join(faltantes)
    await context.bot.send_message(chat_id=chat_id, text=msg)
    return True

async def guardar_recordatorio(chat_id, datos):
    """Guarda el recordatorio en Firebase."""
    now = datetime.now(pytz.timezone("America/Lima"))
    datos["fecha_creacion"] = now.isoformat()
    db.collection("recordatorios").add(datos)

async def agendar_notificacion(context, chat_id, datos):
    """Agenda los mensajes de notificación."""
    fecha_hora = parse_fecha_hora(datos["fecha_hora"])
    if not fecha_hora:
        return
    now = datetime.now(pytz.timezone("America/Lima"))
    delta_principal = (fecha_hora - now).total_seconds()
    delta_previa = delta_principal - 600  # 10 minutos antes

    # Notificación 10 minutos antes
    if delta_previa > 0:
        context.job_queue.run_once(
            lambda ctx: ctx.bot.send_message(chat_id=chat_id, text=f"⏰ Recordatorio: Faltan 10 minutos para tu cita: {datos}"),
            when=delta_previa,
            chat_id=chat_id
        )
    # Notificación principal
    if delta_principal > 0:
        context.job_queue.run_once(
            lambda ctx: ctx.bot.send_message(chat_id=chat_id, text=f"🚨 ¡Es hora de tu cita!: {datos}"),
            when=delta_principal,
            chat_id=chat_id
        )

async def procesar_recordatorio(update: Update, context: ContextTypes.DEFAULT_TYPE, texto):
    chat_id = update.effective_chat.id
    datos = extraer_datos(texto)
    user_states[chat_id] = datos
    if await pedir_datos_faltantes(context, chat_id, datos):
        return
    await guardar_recordatorio(chat_id, datos)
    await agendar_notificacion(context, chat_id, datos)
    await context.bot.send_message(chat_id=chat_id, text="✅ Recordatorio guardado correctamente.")

async def mensaje_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    texto = update.message.text
    # Si hay campos faltantes, continuar llenando
    if chat_id in user_states and any(not v for v in user_states[chat_id].values()):
        datos = user_states[chat_id]
        # Rellena los campos por orden
        for campo in CAMPOS:
            if not datos[campo]:
                datos[campo] = texto.strip()
                break
        user_states[chat_id] = datos
        if await pedir_datos_faltantes(context, chat_id, datos):
            return
        await guardar_recordatorio(chat_id, datos)
        await agendar_notificacion(context, chat_id, datos)
        await context.bot.send_message(chat_id=chat_id, text="✅ Recordatorio guardado correctamente.")
        user_states.pop(chat_id)
        return

    # Procesar como recordatorio por defecto
    await procesar_recordatorio(update, context, texto)

async def citas_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args:
        fecha_str = " ".join(context.args)
    else:
        await context.bot.send_message(chat_id=chat_id, text="Usa: /citas [fecha]. Ejemplo: /citas 2025-06-03")
        return
    dt = parse_fecha_hora(fecha_str)
    if not dt:
        await context.bot.send_message(chat_id=chat_id, text="No entendí la fecha. Intenta con otro formato.")
        return
    fecha_iso = dt.date().isoformat()
    citas = db.collection("recordatorios").where("fecha_hora", ">=", fecha_iso).stream()
    citas_lista = [c.to_dict() for c in citas if c.to_dict().get("fecha_hora", "").startswith(fecha_iso)]
    if not citas_lista:
        await context.bot.send_message(chat_id=chat_id, text=f"No tienes recordatorios para {fecha_iso}.")
        return
    msg = "\n\n".join([f"🗓️ {c['fecha_hora']} - {c['cliente']} ({c.get('motivo', '')})" for c in citas_lista])
    await context.bot.send_message(chat_id=chat_id, text=msg)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("citas", citas_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_handler))
    print("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()

