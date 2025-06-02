import os
import openai
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
from datetime import datetime, timedelta
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

# --- CAMPOS PARA RECORDATORIOS ---
RECORDATORIO_CAMPOS = [
    ("cliente", "¿Cuál es el nombre del cliente?"),
    ("num_cliente", "¿Cuál es el número del cliente (si aplica)?"),
    ("proyecto", "¿Sobre qué proyecto es la reunión o cita?"),
    ("modalidad", "¿Modalidad? (presencial, virtual, reagendar llamada, etc)"),
    ("fecha_hora", "¿Fecha y hora de la cita? (Ej: 2025-06-02 18:00)"),
    ("motivo", "¿Motivo o asunto del recordatorio?"),
    ("observaciones", "¿Alguna observación o detalle especial para este recordatorio? (puedes poner '-' si no hay)"),
]

user_recordatorio = {}  # Guarda temporalmente la info por usuario

# --- EXTRAER FECHA Y HORA CON VERSATILIDAD ---
def parsear_fecha(texto):
    # Soporta: hoy, mañana, fechas, horas sueltas, etc
    texto = texto.lower()
    ahora = datetime.now()
    if "mañana" in texto:
        fecha = ahora + timedelta(days=1)
        return fecha.strftime("%Y-%m-%d")
    match = re.search(r"(\d{4}-\d{2}-\d{2})", texto)
    if match:
        return match.group(1)
    match2 = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", texto)
    if match2:
        d, m, y = match2.group(1).split("/")
        return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return ahora.strftime("%Y-%m-%d")

def parsear_hora(texto):
    match = re.search(r"(\d{1,2})(?:[:h](\d{2}))?\s*(am|pm)?", texto)
    if match:
        hora = int(match.group(1))
        minutos = int(match.group(2) or 0)
        ampm = match.group(3)
        if ampm:
            if ampm.lower() == "pm" and hora < 12:
                hora += 12
            elif ampm.lower() == "am" and hora == 12:
                hora = 0
        return f"{hora:02}:{minutos:02}"
    return "09:00"  # Hora por defecto

def extraer_fecha_hora(texto):
    # "mañana a las 3pm" -> 2025-06-03 15:00
    fecha = parsear_fecha(texto)
    hora = parsear_hora(texto)
    return f"{fecha} {hora}"

# --- FLUJO DE GUARDADO DE RECORDATORIO (UNO A UNO) ---
async def pedir_siguiente_dato(user_id, update, estado):
    for campo, pregunta in RECORDATORIO_CAMPOS:
        if campo not in estado or not estado[campo]:
            await update.message.reply_text(pregunta)
            return campo
    return None  # Todos los campos completos

async def completar_recordatorio(update: Update, estado):
    # Guarda en Firestore y limpia el estado del usuario
    db.collection("recordatorios").add(estado)
    await update.message.reply_text("✅ Recordatorio guardado. Pronto te avisaré aquí mismo.")
    user_recordatorio.pop(update.message.from_user.id, None)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    user_name = user.username or ""
    text = update.message.text.strip()

    # --- FLUJO DE CAPTURA DE RECORDATORIO EN CURSO ---
    if user_id in user_recordatorio:
        estado = user_recordatorio[user_id]
        campo_actual = next((c for c, _ in RECORDATORIO_CAMPOS if not estado.get(c)), None)
        if campo_actual:
            estado[campo_actual] = text
            # Pide el siguiente campo pendiente
            siguiente = await pedir_siguiente_dato(user_id, update, estado)
            if not siguiente:
                # Si ya está completo
                estado["user_id"] = user_id
                estado["usuario"] = user.full_name
                await completar_recordatorio(update, estado)
        return

    # --- INICIO: FLUJO DE AGENDADO ---
    if any(w in text.lower() for w in ["agenda", "recordar", "cita"]):
        # Intenta extraer algunos campos directamente
        estado = {c: "" for c, _ in RECORDATORIO_CAMPOS}
        estado["user_id"] = user_id
        estado["usuario"] = user.full_name

        # Extrae info básica automática (mejorable con GPT, aquí simple)
        if "con" in text.lower():
            try:
                estado["cliente"] = text.lower().split("con", 1)[1].split()[0:3]
                estado["cliente"] = " ".join(estado["cliente"])
            except: pass
        if "para" in text.lower():
            try:
                estado["motivo"] = text.lower().split("para", 1)[1].split()[0:8]
                estado["motivo"] = " ".join(estado["motivo"])
            except: pass
        # Fecha/hora
        if any(w in text.lower() for w in ["hoy", "mañana", "pm", "am", "/", "-"]):
            estado["fecha_hora"] = extraer_fecha_hora(text)
        # Pregunta los campos faltantes
        user_recordatorio[user_id] = estado
        await pedir_siguiente_dato(user_id, update, estado)
        return

    # --- CONSULTA DE RECORDATORIOS POR FECHA ---
    if ("recordatorio" in text.lower() or "pendiente" in text.lower() or "reunión" in text.lower()) and ("hoy" in text.lower() or "mañana" in text.lower() or re.search(r"\d{1,2}/\d{1,2}/\d{4}", text) or re.search(r"\d{4}-\d{2}-\d{2}", text)):
        # Determina fecha buscada
        fecha_buscar = parsear_fecha(text)
        docs = db.collection("recordatorios").where("user_id", "==", user_id).stream()
        lista = []
        for doc in docs:
            data = doc.to_dict()
            if data.get("fecha_hora", "").startswith(fecha_buscar):
                desc = f"{data.get('fecha_hora', '')} - cliente {data.get('cliente', '')} - motivo: {data.get('motivo', '')}"
                lista.append(desc)
        if lista:
            await update.message.reply_text("📅 Tus recordatorios para " + fecha_buscar + ":\n" + "\n".join(lista))
        else:
            await update.message.reply_text(f"No tienes recordatorios para {fecha_buscar}.")
        return

    if "recordatorio" in text.lower() and "tengo" in text.lower():
        # Lista TODOS los recordatorios
        docs = db.collection("recordatorios").where("user_id", "==", user_id).stream()
        lista = []
        for doc in docs:
            data = doc.to_dict()
            desc = f"{data.get('fecha_hora','')} - cliente {data.get('cliente','')} - motivo: {data.get('motivo','')}"
            lista.append(desc)
        if lista:
            await update.message.reply_text("📅 Todos tus recordatorios:\n" + "\n".join(lista))
        else:
            await update.message.reply_text("No tienes ningún recordatorio guardado.")
        return

    # --- CHAT NORMAL GPT-4o (BLUE) ---
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
            {"role": "user", "content": update.message.text}
        ]
    )
    gpt_reply = response.choices[0].message.content.strip()
    await update.message.reply_text(gpt_reply)

    # Guarda chat en firestore
    db.collection("chats").add({
        "user_id": user_id,
        "user_name": user_name,
        "mensaje": update.message.text,
        "respuesta": gpt_reply,
        "fecha": datetime.now()
    })

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Blue listo y corriendo.")
    app.run_polling()

