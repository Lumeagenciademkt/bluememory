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

CAMPOS = ["cliente", "num_cliente", "proyecto", "modalidad", "fecha_hora", "observaciones"]
user_states = {}

def prompt_gpt_neomind(texto, chat_hist=None):
    prompt = f"""
Eres un asistente que ayuda a organizar y consultar recordatorios. Dado el mensaje del usuario, responde solo con este JSON:
{{
  "intencion": "consultar" | "agendar" | "otro",
  "fecha": "",  // Si es consulta, extrae la fecha si la hay. Si no, deja vacío.
  "campos": {{
    "cliente": "",
    "num_cliente": "",
    "proyecto": "",
    "modalidad": "",
    "fecha_hora": "",
    "observaciones": ""
  }}
}}
Si no entiendes, pon "intencion":"otro".

Ejemplo:
Usuario: "Quiero saber mis recordatorios para el viernes" → intencion: "consultar", fecha: "próximo viernes", campos: {{}}
Usuario: "Agenda cita con Juan mañana a las 2" → intencion: "agendar", fecha: "", campos: {{...}}
Usuario: "¿Cuántos planetas tiene Marte?" → intencion: "otro"

Mensaje del usuario: {texto}
JSON:
"""
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0
    )
    content = response.choices[0].message.content
    match = re.search(r'\{[\s\S]+\}', content)
    if match:
        return json.loads(match.group(0))
    return {"intencion": "otro", "fecha": "", "campos": {k:"" for k in CAMPOS}}

def parse_fecha_gpt(fecha_str):
    if not fecha_str:
        return None
    if fecha_str.strip().lower() in ["hoy", "ahora"]:
        return datetime.now(pytz.timezone("America/Lima")).date()
    if fecha_str.strip().lower() == "mañana":
        return (datetime.now(pytz.timezone("America/Lima")) + timedelta(days=1)).date()
    dt = dateparser.parse(fecha_str, languages=['es'])
    if dt:
        return dt.date()
    return None

def parse_fecha_hora_gpt(fecha_str):
    if not fecha_str:
        return None
    dt = dateparser.parse(fecha_str, languages=['es'])
    if dt and dt.tzinfo is None:
        dt = pytz.timezone("America/Lima").localize(dt)
    return dt

def build_resumen(datos):
    fecha_legible = datos.get("fecha_hora", "")
    dt = parse_fecha_hora_gpt(fecha_legible)
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

async def consulta_citas(update, context, fecha=None):
    chat_id = update.effective_chat.id
    now = datetime.now(pytz.timezone("America/Lima"))
    hoy = now.date().isoformat()

    if fecha:
        fecha_iso = fecha.isoformat()
        citas = db.collection("recordatorios").where("fecha_hora", ">=", fecha_iso).stream()
        citas_lista = [c.to_dict() for c in citas if c.to_dict().get("fecha_hora", "").startswith(fecha_iso)]
        msg_head = f"Recordatorios para {fecha.strftime('%d de %B de %Y')}:"
    else:
        # Próximos desde hoy
        citas = db.collection("recordatorios").where("fecha_hora", ">=", hoy).stream()
        citas_lista = [c.to_dict() for c in citas]
        msg_head = "Tus recordatorios pendientes:"

    if not citas_lista:
        msg = f"No tienes recordatorios{' para esa fecha' if fecha else ' pendientes'}."
    else:
        msg = msg_head + "\n\n"
        for c in citas_lista:
            f = c.get("fecha_hora", "")[:16].replace("T", " ")
            msg += f"🗓️ {f} - {c.get('cliente','')} ({c.get('proyecto','')})\nObs: {c.get('observaciones','')}\n\n"
    await update.message.reply_text(msg)

async def responder_gpt(update, texto):
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": texto}],
        temperature=0.5
    )
    await update.message.reply_text(response.choices[0].message.content.strip())

async def mensaje_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    texto = update.message.text.strip()

    if chat_id not in user_states:
        user_states[chat_id] = {}

    estado = user_states[chat_id].get("estado", None)

    # Confirmación para guardar recordatorio
    if estado == "confirmar":
        if texto.lower() in ["sí", "si", "ok", "dale", "confirmo"]:
            datos = user_states[chat_id]["datos"]
            now = datetime.now(pytz.timezone("America/Lima"))
            datos["fecha_creacion"] = now.isoformat()
            db.collection("recordatorios").add(datos)
            user_states[chat_id] = {}
            await update.message.reply_text("✅ ¡Recordatorio guardado! Te avisaré a la hora indicada y 10 minutos antes.")
            return
        elif texto.lower() in ["no", "cambiar", "editar", "modificar"]:
            await update.message.reply_text("OK, vuelve a escribir la información de tu recordatorio, todos los campos o sólo los que quieras cambiar.")
            user_states[chat_id]["estado"] = "pendiente"
            return
        else:
            # Si manda información nueva, reintentar extracción y confirmación
            gpt_result = prompt_gpt_neomind(texto)
            datos = gpt_result.get("campos", {})
            resumen = build_resumen(datos)
            user_states[chat_id]["datos"] = datos
            await update.message.reply_text(resumen)
            return

    # Modo Neomind: interpretación global de la intención (via GPT-4o)
    gpt_result = prompt_gpt_neomind(texto)

    if gpt_result["intencion"] == "consultar":
        fecha = parse_fecha_gpt(gpt_result.get("fecha", ""))
        await consulta_citas(update, context, fecha)
        return

    if gpt_result["intencion"] == "agendar":
        datos = gpt_result["campos"]
        if all(datos.get(k, "") for k in CAMPOS):
            resumen = build_resumen(datos)
            user_states[chat_id]["datos"] = datos
            user_states[chat_id]["estado"] = "confirmar"
            await update.message.reply_text(resumen)
            return
        else:
            faltantes = [k for k in CAMPOS if not datos.get(k, "")]
            if len(faltantes) > 1:
                msg = "Por favor, indícame los siguientes datos:\n" + "\n".join([f"- {campo.replace('_', ' ').capitalize()}" for campo in faltantes])
            else:
                msg = "Por favor, indícame:\n" + "\n".join([f"- {campo.replace('_', ' ').capitalize()}" for campo in faltantes])
            user_states[chat_id]["datos"] = datos
            user_states[chat_id]["estado"] = "pendiente"
            await update.message.reply_text(msg)
            return

    # Si no entiende la intención, responde como ChatGPT
    await responder_gpt(update, texto)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_handler))
    print("Bot Neomind iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()

