import os
import openai
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
from dateutil import parser as dateparser
import datetime
import asyncio

# Cargar variables de entorno
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

openai.api_key = OPENAI_API_KEY

# Inicializar Firebase Admin
cred = credentials.Certificate(GOOGLE_CREDS_JSON)
firebase_admin.initialize_app(cred)
db = firestore.client()

# Estado temporal para cada usuario
user_states = {}

# Campos requeridos para registrar una cita
CAMPOS = [
    ("cliente", "¿Cuál es el nombre del cliente?"),
    ("num_cliente", "¿Cuál es el número del cliente (si aplica)?"),
    ("proyecto", "¿Sobre qué proyecto es la reunión?"),
    ("modalidad", "¿Modalidad? (presencial, virtual, etc)"),
    ("fecha_hora", "¿Fecha y hora de la reunión? (Ej: 2025-06-02 18:00)"),
    ("observaciones", "¿Alguna observación o detalle especial para este recordatorio? (puedes poner '-' si no hay)")
]

# -------------------- FUNCIONES FIREBASE --------------------

def guardar_cita(user, data):
    """Guarda una cita en Firestore."""
    data['usuario'] = user
    db.collection("citas").add(data)

def buscar_citas_por_fecha(fecha_busqueda):
    """Busca citas en una fecha específica."""
    results = []
    citas_ref = db.collection("citas").stream()
    for c in citas_ref:
        cita = c.to_dict()
        try:
            fecha_cita = dateparser.parse(cita['fecha_hora'])
            if fecha_cita.date() == fecha_busqueda.date():
                results.append(cita)
        except:
            continue
    return results

def buscar_citas_por_cliente(nombre_cliente):
    """Busca citas por nombre del cliente."""
    citas_ref = db.collection("citas").where("cliente", "==", nombre_cliente).stream()
    return [c.to_dict() for c in citas_ref]

def todas_las_citas():
    """Lista todas las citas."""
    return [c.to_dict() for c in db.collection("citas").stream()]

# -------------------- FLUJO TELEGRAM --------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    username = user.username or user.first_name or "sin_usuario"
    text = update.message.text.strip().lower()

    # Reseteo del flujo
    if text in ["/reset", "reset", "cancelar"]:
        user_states[user_id] = {}
        await update.message.reply_text("¡Flujo reiniciado! Puedes empezar de nuevo o preguntarme lo que sea.")
        return

    # --- Consulta de citas ---
    if "citas hoy" in text:
        hoy = datetime.datetime.now()
        citas = buscar_citas_por_fecha(hoy)
        if citas:
            msg = "📅 Citas para hoy:\n" + "\n".join([f"- {c['cliente']} ({c['proyecto']}) {c['fecha_hora']}" for c in citas])
        else:
            msg = "No hay citas para hoy."
        await update.message.reply_text(msg)
        return

    if text.startswith("citas ") and len(text.split()) == 2:
        try:
            fecha = dateparser.parse(text.split()[1])
            citas = buscar_citas_por_fecha(fecha)
            if citas:
                msg = f"📅 Citas para {fecha.strftime('%Y-%m-%d')}:\n" + "\n".join([f"- {c['cliente']} ({c['proyecto']}) {c['fecha_hora']}" for c in citas])
            else:
                msg = "No hay citas para esa fecha."
            await update.message.reply_text(msg)
        except:
            await update.message.reply_text("No entendí la fecha. Usa el formato: citas 2025-06-02")
        return

    if text.startswith("buscar "):
        cliente = text.replace("buscar ", "").strip()
        citas = buscar_citas_por_cliente(cliente)
        if citas:
            msg = f"🔎 Citas encontradas para {cliente}:\n" + "\n".join([f"- {c['cliente']} ({c['proyecto']}) {c['fecha_hora']}" for c in citas])
        else:
            msg = f"No hay citas encontradas para {cliente}."
        await update.message.reply_text(msg)
        return

    if "todas las citas" in text or "ver todas" in text:
        citas = todas_las_citas()
        if citas:
            msg = "📑 Todas las citas:\n" + "\n".join([f"- {c['cliente']} ({c['proyecto']}) {c['fecha_hora']}" for c in citas])
        else:
            msg = "No hay citas registradas."
        await update.message.reply_text(msg)
        return

    # --- Registro de citas ---
    if user_id not in user_states:
        user_states[user_id] = {}

    estado = user_states[user_id]

    for campo, pregunta in CAMPOS:
        if campo not in estado or not estado[campo]:
            estado[campo] = update.message.text
            if campo != CAMPOS[-1][0]:
                await update.message.reply_text(pregunta)
            break

    # Si todos los campos están llenos, guarda la cita
    if all([estado.get(campo[0], "") for campo in CAMPOS]):
        cita_data = {campo[0]: estado[campo[0]] for campo in CAMPOS}
        guardar_cita(username, cita_data)
        await update.message.reply_text(
            f"✅ ¡Cita registrada para {cita_data['cliente']} el {cita_data['fecha_hora']}!\n"
            f"Proyecto: {cita_data['proyecto']}\n"
            f"Modalidad: {cita_data['modalidad']}\n"
            f"Observaciones: {cita_data['observaciones']}\n"
        )
        user_states[user_id] = {}
        return

    # --- Conversación normal con GPT ---
    if not any(x in text for x in ["citas", "cliente", "proyecto", "modalidad", "observacion"]):
        # Respuesta casual con GPT
        try:
            respuesta = openai.ChatCompletion.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": update.message.text}]
            )
            await update.message.reply_text(respuesta.choices[0].message.content.strip())
        except Exception as e:
            await update.message.reply_text("¡Ups! No pude responder en este momento. Intenta de nuevo.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot Blue listo y corriendo.")
    app.run_polling()
