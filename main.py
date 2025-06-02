import os
import openai
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import datetime
import asyncio
from dateutil import parser as dateparser

# ========== Cargar variables de entorno ==========
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FIREBASE_CRED_PATH = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

openai.api_key = OPENAI_API_KEY

# ========== Firebase ==========
cred = credentials.Certificate(FIREBASE_CRED_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()

# ========== Memoria de usuario para flujo de citas ==========
user_states = {}

# ========== Función: detecta si es una pregunta casual ==========
def es_mensaje_casual(text):
    palabras_casuales = ["hola", "cómo estás", "que tal", "cuéntame", "quién eres", "ayuda", "dime", "buenos días", "buenas", "gracias"]
    return any(p in text.lower() for p in palabras_casuales)

# ========== GPT-4 IA ==========
async def respuesta_casual(text):
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Eres un asistente de WhatsApp casual, conversacional, proactivo, breve y amigable. También puedes conversar de temas variados, responder saludos y bromas. Si el usuario te pregunta algo casual, responde como un amigo digital."},
            {"role": "user", "content": text}
        ],
        max_tokens=200,
        temperature=0.6
    )
    return response.choices[0].message.content.strip()

# ========== Flujo de agendar cita ==========
CAMPOS = [
    ("cliente", "¿Cuál es el nombre del cliente?"),
    ("num_cliente", "¿Cuál es el número del cliente (si aplica)?"),
    ("proyecto", "¿Sobre qué proyecto es la reunión?"),
    ("modalidad", "¿Modalidad? (presencial, virtual, llamada, etc)"),
    ("fecha_hora", "¿Fecha y hora de la reunión? (Ej: 2025-06-02 18:00)"),
    ("observaciones", "¿Alguna observación especial? (puedes poner '-' si no hay)")
]

def cita_flujo_incompleto(estado):
    for campo, pregunta in CAMPOS:
        if campo not in estado or not estado[campo]:
            return campo, pregunta
    return None, None

async def guardar_cita(uid, estado):
    cita = estado.copy()
    cita["timestamp"] = datetime.datetime.now().isoformat()
    db.collection("citas").add(cita)

async def buscar_citas_por_fecha(fecha):
    citas_ref = db.collection("citas")
    fecha_inicio = dateparser.parse(fecha).replace(hour=0, minute=0, second=0, microsecond=0)
    fecha_fin = fecha_inicio + datetime.timedelta(days=1)
    docs = citas_ref.where("fecha_hora", ">=", fecha_inicio.isoformat()).where("fecha_hora", "<", fecha_fin.isoformat()).stream()
    return [doc.to_dict() for doc in docs]

async def buscar_citas_por_cliente(nombre):
    citas_ref = db.collection("citas")
    docs = citas_ref.where("cliente", "==", nombre).stream()
    return [doc.to_dict() for doc in docs]

# ========== Handler principal ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    text = update.message.text.strip()
    
    # --- Detecta si está llenando flujo de cita ---
    if user_id in user_states and user_states[user_id]:
        estado = user_states[user_id]
        campo_actual, pregunta = cita_flujo_incompleto(estado)
        if campo_actual:
            estado[campo_actual] = text
            campo_siguiente, pregunta_siguiente = cita_flujo_incompleto(estado)
            if campo_siguiente:
                await update.message.reply_text(pregunta_siguiente)
                return
            else:
                await guardar_cita(user_id, estado)
                await update.message.reply_text("✅ ¡Cita registrada con éxito!\nPuedes consultar tus citas por fecha o cliente cuando quieras.")
                user_states[user_id] = {}
                return
    
    # --- Mensaje de CRM: agendar cita ---
    if "agendar cita" in text.lower() or "nueva cita" in text.lower():
        user_states[user_id] = {}
        await update.message.reply_text("¡Vamos a agendar una nueva cita! " + CAMPOS[0][1])
        return
    
    # --- Buscar citas por fecha ---
    if "citas" in text.lower() and any(char.isdigit() for char in text):
        try:
            fecha = "".join(filter(lambda c: c.isdigit() or c == "-", text))
            citas = await buscar_citas_por_fecha(fecha)
            if citas:
                respuesta = "\n\n".join([f"🗓 {c['fecha_hora']} - {c['cliente']} - {c['proyecto']}" for c in citas])
            else:
                respuesta = "No hay citas encontradas para esa fecha."
            await update.message.reply_text(respuesta)
        except Exception as e:
            await update.message.reply_text("Error al buscar citas por fecha.")
        return

    # --- Buscar citas por cliente ---
    if "buscar" in text.lower():
        nombre = text.lower().replace("buscar", "").strip().title()
        citas = await buscar_citas_por_cliente(nombre)
        if citas:
            respuesta = "\n\n".join([f"🗓 {c['fecha_hora']} - {c['cliente']} - {c['proyecto']}" for c in citas])
        else:
            respuesta = "No hay citas para ese cliente."
        await update.message.reply_text(respuesta)
        return

    # --- Pregunta casual: usa GPT-4 ---
    if es_mensaje_casual(text) or len(text.split()) < 5:
        reply = await respuesta_casual(text)
        await update.message.reply_text(reply)
        return

    # --- Respuesta por defecto ---
    await update.message.reply_text("Soy tu asistente CRM. Puedes agendar citas o preguntarme cualquier cosa 😉")

# ========== Start bot ==========
if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot listo y corriendo.")
    app.run_polling()
