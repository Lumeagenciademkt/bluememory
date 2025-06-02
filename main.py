import os
import openai
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
import asyncio
import datetime
import re

# --- Cargar variables de entorno ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

# --- Inicializar Firebase ---
if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# --- Prompt de sistema de Blue ---
SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "Eres Blue, una IA experta en organización y productividad. "
        "Tu objetivo principal es ayudar a los usuarios a organizar sus pendientes, citas y recordatorios. "
        "Si detectas una instrucción relacionada con una cita o recordatorio, extrae la información relevante "
        "(texto, fecha, hora) y pídela si falta algún dato. "
        "Si te preguntan por pendientes, responde con la información almacenada. "
        "El resto del tiempo puedes conversar normalmente, pero tu especialidad es organizar agendas."
    )
}

# --- Utilidades ---
def parse_datetime(text):
    """Extrae fecha y hora desde texto libre usando regex simple, puede mejorarse con GPT-4."""
    # Buscar formato tipo: "mañana a las 3pm", "hoy a las 18:00", "el 5 de junio a las 8am"
    now = datetime.datetime.now()
    patterns = [
        (r"mañana.*?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?", 1),
        (r"hoy.*?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?", 0),
        (r"(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?.*?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?", None),
    ]
    for pat, plus_days in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            if pat.startswith("mañana"):
                base = now + datetime.timedelta(days=1)
            elif pat.startswith("hoy"):
                base = now
            else:
                # formato dd/mm/aaaa hh:mm
                try:
                    day, month, year = int(m.group(1)), int(m.group(2)), now.year
                    if m.lastindex >= 3 and m.group(3):
                        year = int(m.group(3))
                    hour = int(m.group(4))
                    minute = int(m.group(5)) if m.lastindex >= 5 and m.group(5) else 0
                    if m.lastindex >= 6 and m.group(6):
                        if "pm" in m.group(6).lower() and hour < 12:
                            hour += 12
                    dt = datetime.datetime(year, month, day, hour, minute)
                    return dt
                except Exception:
                    continue
            hour = int(m.group(1))
            minute = int(m.group(2)) if m.lastindex >= 2 and m.group(2) else 0
            if m.lastindex >= 3 and m.group(3):
                if "pm" in m.group(3).lower() and hour < 12:
                    hour += 12
            dt = datetime.datetime(base.year, base.month, base.day, hour, minute)
            return dt
    return None

async def avisar_recordatorios(app):
    """Chequea recordatorios futuros cada minuto y avisa si corresponde."""
    while True:
        ahora = datetime.datetime.now()
        pendientes = db.collection("recordatorios").where("avisado", "==", False).stream()
        for doc in pendientes:
            data = doc.to_dict()
            hora_recordatorio = data.get("fecha_hora")
            if hora_recordatorio:
                dt = datetime.datetime.strptime(hora_recordatorio, "%Y-%m-%d %H:%M")
                if (dt - ahora).total_seconds() < 60 and (dt - ahora).total_seconds() > -60:
                    # Enviar aviso al usuario
                    await app.bot.send_message(
                        chat_id=data["user_id"],
                        text=f"🔔 ¡Recordatorio!: {data['texto']} (para {dt.strftime('%Y-%m-%d %H:%M')})"
                    )
                    db.collection("recordatorios").document(doc.id).update({"avisado": True})
        await asyncio.sleep(30)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(update.message.chat_id)
    text = update.message.text

    # 1. Chequear si el usuario pregunta por pendientes/citas
    if "pendiente" in text.lower() or "cita" in text.lower() or "reunión" in text.lower() or "agenda" in text.lower():
        hoy = datetime.datetime.now().strftime("%Y-%m-%d")
        query = db.collection("recordatorios").where("user_id", "==", user_id).where("fecha_hora", ">=", f"{hoy} 00:00").where("fecha_hora", "<=", f"{hoy} 23:59")
        docs = list(query.stream())
        if docs:
            respuesta = "Estos son tus pendientes/citas de hoy:\n"
            for doc in docs:
                dato = doc.to_dict()
                respuesta += f"- {dato['texto']} ({dato['fecha_hora']})\n"
        else:
            respuesta = "No tienes pendientes registrados para hoy."
        await update.message.reply_text(respuesta)
        return

    # 2. Intentar detectar si es un recordatorio para guardar
    if "recuérdame" in text.lower() or "recordar" in text.lower():
        # Extraer texto y fecha/hora con GPT-4o para máxima precisión
        gpt_prompt = [
            SYSTEM_PROMPT,
            {"role": "user", "content": f"Detecta si hay que guardar un recordatorio con texto, fecha y hora del siguiente mensaje: '{text}'. "
                                        "Responde solo con un JSON del tipo: {\"texto\": \"...\", \"fecha_hora\": \"AAAA-MM-DD HH:MM\"}. Si no hay fecha/hora, escribe null"}
        ]
        try:
            completion = openai.chat.completions.create(
                model="gpt-4o",
                messages=gpt_prompt
            )
            import json
            respuesta_json = completion.choices[0].message.content.strip()
            data = json.loads(respuesta_json)
            texto = data.get("texto")
            fecha_hora = data.get("fecha_hora")
        except Exception as e:
            texto = text
            fecha_hora = None

        # Si no hay fecha/hora, pedirla
        if not fecha_hora:
            await update.message.reply_text("¿Para qué fecha y hora debo programar el recordatorio? Ejemplo: 2025-06-02 18:00")
            context.user_data["pendiente"] = texto
            return

        # Guardar en Firestore
        db.collection("recordatorios").add({
            "user_id": user_id,
            "user_name": user.username or user.full_name,
            "texto": texto,
            "fecha_hora": fecha_hora,
            "avisado": False
        })
        await update.message.reply_text(f"✅ ¡Recordatorio guardado para {fecha_hora}!")

        return

    # 3. Si el usuario responde con fecha/hora tras haberle preguntado antes
    if "pendiente" in context.user_data and re.search(r"\d{4}-\d{2}-\d{2}", text):
        texto = context.user_data.pop("pendiente")
        fecha_hora = text.strip()
        db.collection("recordatorios").add({
            "user_id": user_id,
            "user_name": user.username or user.full_name,
            "texto": texto,
            "fecha_hora": fecha_hora,
            "avisado": False
        })
        await update.message.reply_text(f"✅ ¡Recordatorio guardado para {fecha_hora}!")
        return

    # 4. Si no, chat normal (pero siempre rol Blue)
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            SYSTEM_PROMPT,
            {"role": "user", "content": text}
        ]
    )
    gpt_reply = response.choices[0].message.content.strip()
    await update.message.reply_text(gpt_reply)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Blue listo y corriendo con recordatorios reales.")
    loop = asyncio.get_event_loop()
    loop.create_task(avisar_recordatorios(app))
    app.run_polling()
