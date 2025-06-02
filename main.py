import os
import openai
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv

# Firebase (solo para guardar/leer recordatorios)
import firebase_admin
from firebase_admin import credentials, firestore

# === Configuración básica ===
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    text = update.message.text

    # Ejemplo de guardar recordatorio con una keyword simple
    if text.lower().startswith("recordar") or "cita" in text.lower():
        doc = db.collection("recordatorios").add({
            "user_id": user_id,
            "texto": text
        })
        await update.message.reply_text("✅ Recordatorio guardado. Pronto te avisaré aquí mismo.")
        return

    # Consulta de pendientes
    if "pendiente" in text.lower() or "tengo hoy" in text.lower():
        docs = db.collection("recordatorios").where("user_id", "==", user_id).stream()
        pendientes = [d.to_dict()["texto"] for d in docs]
        if pendientes:
            await update.message.reply_text("Tus pendientes:\n" + "\n".join(f"- {p}" for p in pendientes))
        else:
            await update.message.reply_text("No tienes pendientes guardados.")
        return

    # Chat normal con GPT
    prompt = f"Eres Blue, un asistente IA para organización de tareas, recordatorios y productividad. Responde de manera útil, pero también puedes conversar casualmente si el usuario lo desea.\n\nUsuario: {text}\nBlue:"
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": "Eres Blue, una IA de organización y recordatorios en Telegram."},
                  {"role": "user", "content": text}]
    )
    gpt_reply = response.choices[0].message.content.strip()
    await update.message.reply_text(gpt_reply)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Blue bot activo.")
    app.run_polling()

