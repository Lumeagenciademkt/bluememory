import os
import openai
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
from datetime import datetime
import pytz
import dateparser
import re
import json

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# Sin 'motivo'
CAMPOS = ["cliente", "num_cliente", "proyecto", "modalidad", "fecha_hora", "observaciones"]

# Memoria de usuario (hasta 20 mensajes)
user_states = {}

def extraer_intencion(texto):
    patrones = ["agenda", "agendar", "cita", "recordatorio", "reunión", "reunion", "recuerda", "avísame"]
    return any(pat in texto.lower() for pat in patrones)

def extraer_datos_gpt(texto):
    prompt = f"""Eres un asistente que organiza recordatorios. Extrae los siguientes campos (de manera flexible y humana) del usuario:
Cliente, Número de cliente, Proyecto, Modalidad (presencial/virtual), Fecha y hora, Observaciones.
Devuelve SOLO el siguiente JSON (deja vacío si no hay info):
{{
    "cliente": "",
    "num_cliente": "",
    "proyecto": "",
    "modalidad": "",
    "fecha_hora": "",
    "observaciones": ""
}}
Mensaje del usuario: {texto}
JSON:"""
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0
    )
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

def build_resumen(datos):
    fecha_legible = datos.get("fecha_hora", "")
    dt = parse_fecha_hora(fecha_legible)
    if dt:
        fecha_legible = dt.strftime("%d de %B de %Y, %I:%M %p")
        datos["fecha_hora"] = dt.isoformat()
    return (
        f"Perfecto, esto es lo que entendí:\n"
        f"- Cliente: {datos.get('cliente','')}\n"
        f"- Número de cliente: {datos.get('num_cliente','')}\n"
        f"- Proyecto: {datos.get('proyecto','')}\n"
        f"- Modalidad: {datos.get('modalidad','')}\n"
        f"- Fecha y hora: {fecha_legible}\n"
        f"- Observaciones: {datos.get('observaciones','')}\n\n"
        "¿Está correcto? (Responde 'sí' para guardar, o dime qué cambiar)"
    )

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

    # Prepara historial de usuario
    if chat_id not in user_states:
        user_states[chat_id] = {"hist": []}
    # Memoria corta: guarda los últimos 20 mensajes del usuario
    user_states[chat_id]["hist"].append(texto)
    user_states[chat_id]["hist"] = user_states[chat_id]["hist"][-20:]

    estado = user_states[chat_id].get("estado", None)

    # Confirmación de guardado
    if estado == "confirmar":
        if texto.lower() in ["sí", "si", "ok", "dale", "confirmo"]:
            datos = user_states[chat_id]["datos"]
            now = datetime.now(pytz.timezone("America/Lima"))
            datos["fecha_creacion"] = now.isoformat()
            db.collection("recordatorios").add(datos)
            user_states[chat_id] = {"hist": user_states[chat_id]["hist"]}
            await update.message.reply_text("✅ ¡Recordatorio guardado! Te avisaré a la hora indicada y 10 minutos antes.")
            return
        elif texto.lower() in ["no", "cambiar", "editar", "modificar"]:
            await update.message.reply_text("OK, vuelve a escribir la información de tu recordatorio, todos los campos o sólo los que quieras cambiar.")
            user_states[chat_id]["estado"] = "pendiente"
            return
        else:
            # Si manda información nueva, reintentar extracción y confirmación
            datos = extraer_datos_gpt(texto)
            resumen = build_resumen(datos)
            user_states[chat_id]["datos"] = datos
            await update.message.reply_text(resumen)
            return

    # Flujo: agendar recordatorio (intención o estado pendiente)
    if extraer_intencion(texto) or estado == "pendiente":
        # Analiza todos los mensajes del historial para extraer datos completos
        texto_completo = "\n".join(user_states[chat_id]["hist"])
        datos = extraer_datos_gpt(texto_completo)
        if all(datos[k] for k in CAMPOS):
            resumen = build_resumen(datos)
            user_states[chat_id]["datos"] = datos
            user_states[chat_id]["estado"] = "confirmar"
            await update.message.reply_text(resumen)
            return
        else:
            # Si falta algún dato, pide sólo lo faltante
            faltantes = [k for k in CAMPOS if not datos[k]]
            msg = "Por favor, indícame: " + ", ".join(faltantes)
            user_states[chat_id]["datos"] = datos
            user_states[chat_id]["estado"] = "pendiente"
            await update.message.reply_text(msg)
            return

    # Default: Chat normal
    user_states[chat_id]["estado"] = None
    await responder_gpt(update, texto)

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
    msg = "\n\n".join([f"🗓️ {c['fecha_hora']} - {c['cliente']} ({c.get('observaciones', '')})" for c in citas_lista])
    await update.message.reply_text(msg)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("citas", citas_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_handler))
    print("Bot iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
