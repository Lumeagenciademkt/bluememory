import os
import openai
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
import dateparser
import re
import json

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

# --- Estados en memoria por usuario ---
user_states = {}

# --- Campos de recordatorio en orden ---
CAMPOS = [
    ("cliente", "¿Cuál es el nombre del cliente?"),
    ("num_cliente", "¿Cuál es el número del cliente (si aplica)?"),
    ("proyecto", "¿Sobre qué proyecto es la reunión o cita?"),
    ("modalidad", "¿Modalidad? (presencial, virtual, reagendar llamada, etc)"),
    ("fecha_hora", "¿Fecha y hora de la cita? (Ej: 2025-06-02 18:00 o 'mañana 3pm')"),
    ("motivo", "¿Motivo o asunto del recordatorio?"),
    ("observaciones", "¿Alguna observación o detalle especial para este recordatorio? (puedes poner '-' si no hay)")
]

# --- GPT Extraction (pide a GPT separar los campos del texto) ---
def gpt_extract_fields(mensaje):
    prompt = f"""
Eres un asistente para agendas empresariales. Dado el siguiente mensaje de un usuario, extrae estos campos:
- cliente
- num_cliente
- proyecto
- modalidad
- fecha_hora
- motivo
- observaciones

Si un campo no está presente, escribe "FALTA".

Mensaje: \"{mensaje}\"
Responde solo con un JSON.
"""
    respuesta = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    try:
        fields = json.loads(respuesta.choices[0].message.content)
    except Exception:
        fields = {}
    return fields

# --- Normaliza fecha y hora (flexible, output ISO local) ---
def normaliza_fecha(texto):
    tz = pytz.timezone('America/Lima')
    dt = dateparser.parse(
        texto,
        settings={'TIMEZONE': 'America/Lima', 'RETURN_AS_TIMEZONE_AWARE': True}
    )
    if not dt:
        return texto
    dt = dt.astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M")

# --- Flujo de llenado de recordatorio ---
async def wizard_recordatorio(update, estado, text):
    fields = gpt_extract_fields(text)
    # Actualiza los campos si los encuentra
    for campo, _ in CAMPOS:
        valor = fields.get(campo)
        if valor and valor != "FALTA":
            if campo == "fecha_hora":
                valor = normaliza_fecha(valor)
            estado[campo] = valor

    # Pide el siguiente campo pendiente
    for campo, pregunta in CAMPOS:
        if campo not in estado or not estado[campo]:
            await update.message.reply_text(pregunta)
            estado["last_campo"] = campo
            return False
    return True

# --- Consulta recordatorios por fecha ---
def consulta_recordatorios(user_id, fecha_buscada):
    docs = db.collection("recordatorios").where("user_id", "==", user_id).stream()
    result = []
    for doc in docs:
        data = doc.to_dict()
        fecha_hora = data.get("fecha_hora", "")
        if fecha_hora.startswith(fecha_buscada):
            result.append(data)
    return result

# --- Handler principal ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    text = update.message.text.strip()
    user_name = user.username or user.first_name or "usuario"

    # --- Maneja estados por usuario (wizard para recordatorios) ---
    estado = user_states.get(user_id, {})
    if estado.get("en_proceso"):
        # Está llenando campos pendientes
        last = estado.get("last_campo")
        if last:
            valor = text
            if last == "fecha_hora":
                valor = normaliza_fecha(valor)
            estado[last] = valor
        completos = True
        for campo, pregunta in CAMPOS:
            if campo not in estado or not estado[campo]:
                await update.message.reply_text(pregunta)
                estado["last_campo"] = campo
                completos = False
                break
        if completos:
            db.collection("recordatorios").add({
                "user_id": user_id,
                "usuario": user_name,
                "cliente": estado.get("cliente", ""),
                "num_cliente": estado.get("num_cliente", ""),
                "proyecto": estado.get("proyecto", ""),
                "modalidad": estado.get("modalidad", ""),
                "fecha_hora": estado.get("fecha_hora", ""),
                "motivo": estado.get("motivo", ""),
                "observaciones": estado.get("observaciones", ""),
            })
            await update.message.reply_text("✅ Recordatorio guardado. Pronto te avisaré aquí mismo.")
            user_states[user_id] = {}
        else:
            user_states[user_id] = estado
        return

    # --- Nueva solicitud de recordatorio (one shot, si posible) ---
    if any(x in text.lower() for x in ["agenda", "recordar", "cita", "reunión"]):
        user_states[user_id] = {"en_proceso": True}
        completos = await wizard_recordatorio(update, user_states[user_id], text)
        if completos:
            estado = user_states[user_id]
            db.collection("recordatorios").add({
                "user_id": user_id,
                "usuario": user_name,
                "cliente": estado.get("cliente", ""),
                "num_cliente": estado.get("num_cliente", ""),
                "proyecto": estado.get("proyecto", ""),
                "modalidad": estado.get("modalidad", ""),
                "fecha_hora": estado.get("fecha_hora", ""),
                "motivo": estado.get("motivo", ""),
                "observaciones": estado.get("observaciones", ""),
            })
            await update.message.reply_text("✅ Recordatorio guardado. Pronto te avisaré aquí mismo.")
            user_states[user_id] = {}
        return

    # --- Consulta de recordatorios por fecha (soporta fechas casuales) ---
    if any(x in text.lower() for x in ["recordatorio", "pendiente", "reunión", "qué tengo"]):
        # Extrae fecha buscada
        tz = pytz.timezone('America/Lima')
        now = datetime.now(tz)
        fecha_buscada = None
        # Busca frases tipo "para el 3 de junio", "para mañana", etc.
        if "mañana" in text.lower():
            fecha_buscada = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        elif "hoy" in text.lower():
            fecha_buscada = now.strftime("%Y-%m-%d")
        else:
            fecha_detectada = dateparser.parse(
                text, settings={'TIMEZONE': 'America/Lima', 'RETURN_AS_TIMEZONE_AWARE': True}
            )
            if fecha_detectada:
                fecha_buscada = fecha_detectada.astimezone(tz).strftime("%Y-%m-%d")
            else:
                fecha_buscada = now.strftime("%Y-%m-%d")
        recordatorios = consulta_recordatorios(user_id, fecha_buscada)
        if recordatorios:
            lista = [
                f"{r.get('fecha_hora','')} - {r.get('motivo','')} {r.get('cliente','')}"
                for r in recordatorios
            ]
            await update.message.reply_text("📅 Tus recordatorios para esa fecha:\n" + "\n".join(lista))
        else:
            await update.message.reply_text("No tienes recordatorios para esa fecha. ¿Quieres agendar uno nuevo?")
        return

    # --- Chat normal GPT-4o ---
    system_prompt = (
        "Eres Blue, un asistente personal para organización en Telegram. "
        "Tu objetivo principal es ayudar al usuario a organizar su día, agendar recordatorios y citas, "
        "y responder de forma amistosa, útil y concisa. Si la pregunta es de organización, "
        "siempre ofrece ayuda proactiva, y si es otra cosa responde como un chatbot útil."
    )
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    )
    gpt_reply = response.choices[0].message.content.strip()
    await update.message.reply_text(gpt_reply)

    # Guarda chat en firestore
    db.collection("chats").add({
        "user_id": user_id,
        "user_name": user_name,
        "mensaje": text,
        "respuesta": gpt_reply,
        "fecha": datetime.now()
    })

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Blue listo y corriendo, ahora extrae y pregunta cada campo, filtra bien por fecha, y responde mejor.")
    app.run_polling()
