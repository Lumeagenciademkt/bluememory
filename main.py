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

# --- Función: Extrae posible fecha/hora del mensaje (mejorar a futuro) ---
def extraer_fecha_hora(texto):
    # Ejemplo: "2pm 02/06/2025", "3pm 2 de junio de 2025", "mañana a las 10"
    # Para demo: solo 24h o fechas claras tipo "02/06/2025 15:00"
    match = re.search(r'(\d{1,2}[:h]?\d{0,2})\s*(am|pm)?[ ,de]*([\d/]+)?', texto, re.IGNORECASE)
    if match:
        hora = match.group(1)
        ampm = match.group(2) or ""
        fecha = match.group(3) or datetime.now().strftime("%d/%m/%Y")
        # Normaliza hora
        if ":" not in hora:
            hora = hora + ":00"
        # Convierte fecha al formato yyyy-mm-dd
        if "/" in fecha:
            dia, mes, año = fecha.split("/")
            fecha_fmt = f"{año}-{mes.zfill(2)}-{dia.zfill(2)}"
        else:
            fecha_fmt = datetime.now().strftime("%Y-%m-%d")
        # Convierte a datetime final
        try:
            hora_24 = datetime.strptime(hora + ampm, "%I:%M%p").strftime("%H:%M")
        except:
            hora_24 = hora
        return f"{fecha_fmt} {hora_24}"
    return None

# --- Manejo de mensajes ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    user_name = user.username or ""
    text = update.message.text.strip().lower()

    # --- Reconoce comandos simples ---
    # Guardar recordatorio sencillo
    if "agenda" in text or "recordar" in text or "cita" in text:
        # Ejemplo: "Agendame una cita con Jose mañana a las 2pm"
        # Simple parsing
        fecha_hora = extraer_fecha_hora(text)
        motivo = ""
        cliente = ""
        proyecto = ""
        modalidad = ""
        observaciones = ""

        # Extrae cliente (palabra después de "con")
        if "con" in text:
            cliente = text.split("con",1)[1].split()[0:3]
            cliente = " ".join(cliente)

        # Extrae motivo después de "para"
        if "para" in text:
            motivo = text.split("para",1)[1].split()[0:8]
            motivo = " ".join(motivo)

        # Si no se detectó fecha/hora clara, pide al usuario la fecha
        if not fecha_hora:
            await update.message.reply_text("¿Para cuándo es el recordatorio? (Ej: 02/06/2025 15:00)")
            return

        # Guarda en Firestore
        db.collection("recordatorios").add({
            "user_id": user_id,
            "usuario": user.full_name,
            "cliente": cliente,
            "fecha_hora": fecha_hora,
            "motivo": motivo,
            "proyecto": proyecto,
            "modalidad": modalidad,
            "observaciones": observaciones,
        })
        await update.message.reply_text("✅ Recordatorio guardado. Pronto te avisaré aquí mismo.")

        return

    # Consultar recordatorios de HOY o una fecha específica
    if "recordatorio" in text and ("hoy" in text or "tengo" in text or "pendiente" in text or "reunión" in text):
        # Consulta de Firestore
        hoy = datetime.now().strftime("%Y-%m-%d")
        docs = db.collection("recordatorios").where("user_id", "==", user_id).stream()
        lista = []
        for doc in docs:
            data = doc.to_dict()
            # Filtra por fecha de hoy
            fecha_txt = data.get("fecha_hora","")
            if fecha_txt.startswith(hoy):
                lista.append(f"{data.get('fecha_hora','')} - {data.get('motivo','')} {data.get('cliente','')}")
        if lista:
            await update.message.reply_text("📅 Tus recordatorios para hoy:\n" + "\n".join(lista))
        else:
            await update.message.reply_text("No tienes recordatorios para hoy.")
        return

    # --- Chat IA normal (Blue) ---
    # Personalidad
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

