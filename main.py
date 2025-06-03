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

# Cargar variables de entorno
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

# Inicializa Firebase
if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

CAMPOS = ["cliente", "num_cliente", "proyecto", "modalidad", "fecha_hora", "motivo", "observaciones"]
user_states = {}

def extraer_intencion(texto):
    # Si el texto tiene palabras de agendar, lo detecta como recordatorio
    patrones = ["agenda", "agendar", "cita", "recordatorio", "reunión", "reunion", "recuerda", "avísame"]
    return any(pat in texto.lower() for pat in patrones)

def extraer_datos_gpt(texto):
    prompt = f"""Eres un asistente que organiza recordatorios para humanos muy ocupados. Extrae los siguientes campos de la solicitud del usuario (aunque esté desordenado o falten algunos):
Cliente, Número de cliente, Proyecto, Modalidad (presencial/virtual), Fecha y hora, Motivo, Observaciones.
Devuelve SOLO el siguiente JSON (rellena vacío si no hay dato):
{{
    "cliente": "",
    "num_cliente": "",
    "proyecto": "",
    "modalidad": "",
    "fecha_hora": "",
    "motivo": "",
    "observaciones": ""
}}
Mensaje del usuario: {texto}
JSON:"""
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0
    )
    import json
    content = response.choices[0].message.content
    match = re.search(r'\{[\s\S]+\}', content)
    if match:
        return json.loads(match.group(0))
    return {k: "" for k in CAMPOS}

def parse_fecha_hora(fecha_str):
    if not fecha_str:
        return None
    dt = dateparser.parse(fecha_str, languages=['es'])
    if dt and dt.tzinfo is None:
        dt = pytz.timezone("America/Lima").localize(dt)
    return dt

async def responder_gpt(update, texto):
    # Responde como ChatGPT normal
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": texto}],
        temperature=0.5
    )
    await update.message.reply_text(response.choices[0].message.content.strip())

async def mensaje_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    texto = update.message.text.strip()

    # Flujo: esperando confirmación del usuario para guardar
    if chat_id in user_states and user_states[chat_id].get("estado") == "confirmar":
        if texto.lower() in ["sí", "si", "ok", "dale", "confirmo"]:
            datos = user_states[chat_id]["datos"]
            now = datetime.now(pytz.timezone("America/Lima"))
            datos["fecha_creacion"] = now.isoformat()
            db.collection("recordatorios").add(datos)
            user_states.pop(chat_id)
            await update.message.reply_text("✅ ¡Recordatorio guardado! Te avisaré a la hora indicada y 10 minutos antes.")
            # Aquí deberías programar las notificaciones reales
            return
        else:
            await update.message.reply_text("Entiendo, ¿quieres modificar algo? Por favor envíame el mensaje de nuevo con las correcciones o vuelve a escribir tu cita.")
            user_states.pop(chat_id)
            return

    # Flujo principal: si detecta intención de agendar
    if extraer_intencion(texto):
        datos = extraer_datos_gpt(texto)
        fecha_legible = datos["fecha_hora"]
        dt = parse_fecha_hora(fecha_legible)
        if dt:
            fecha_legible = dt.strftime("%d de %B de %Y, %I:%M %p")
            datos["fecha_hora"] = dt.isoformat()
        resumen = (
            f"Perfecto, entiendo que quieres agendar este recordatorio:\n"
            f"- Cliente: {datos['cliente']}\n"
            f"- Número de cliente: {datos['num_cliente']}\n"
            f"- Proyecto: {datos['proyecto']}\n"
            f"- Modalidad: {datos['modalidad']}\n"
            f"- Fecha y hora: {fecha_legible}\n"
            f"- Motivo: {datos['motivo']}\n"
            f"- Observaciones: {datos['observaciones']}\n\n"
            "¿Está correcto? (Responde 'sí' para guardar, o dime qué cambiar)"
        )
        user_states[chat_id] = {"estado": "confirmar", "datos": datos}
        await update.message.reply_text(resumen)
        return

    # Si no, responde como ChatGPT normal
    await responder_gpt(update, texto)

# Consulta de citas por fecha (básica, mejora según tu flujo)
async def citas_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args:
        fecha_str = " ".join(context.args)
    else:
        await update.message.reply_text("Usa: /citas [fecha]. Ejemplo: /citas 2025-06-03")
        return
    dt = parse_fecha_hora(fecha_str)
    if not dt:
        await update.message.reply_text("No entendí la fecha. Intenta con otro formato.")
        return
    fecha_iso = dt.date().isoformat()
    citas = db.collection("recordatorios").where("fecha_hora", ">=", fecha_iso).stream()
    citas_lista = [c.to_dict() for c in citas if c.to_dict().get("fecha_hora", "").startswith(fecha_iso)]
    if not citas_lista:
        await update.message.reply_text(f"No tienes recordatorios para {fecha_iso}.")
        return
    msg = "\n\n".join([f"🗓️ {c['fecha_hora']} - {c['cliente']} ({c.get('motivo', '')})" for c in citas_lista])
    await update.message.reply_text(msg)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("citas", citas_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_handler))
    print("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
