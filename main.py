import os
import openai
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import asyncio

# ===== Configuración básica =====
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

# ===== Firebase Init =====
if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

esperando_guardado = set()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.message.from_user.id)
    text = update.message.text.strip()
    
    # 1. Comando guardar
    if text.lower() == "guardar":
        esperando_guardado.add(user_id)
        await update.message.reply_text("Escribe el mensaje que quieres guardar:")
        return

    # 2. Si estaba esperando guardar
    if user_id in esperando_guardado:
        db.collection("mensajes").add({
            "user_id": user_id,
            "texto": text
        })
        esperando_guardado.remove(user_id)
        await update.message.reply_text("¡Mensaje guardado en la base de datos! 👍")
        return

    # 3. Comando ver
    if text.lower() == "ver":
        docs = db.collection("mensajes").where("user_id", "==", user_id).stream()
        mensajes = [doc.to_dict()["texto"] for doc in docs]
        if mensajes:
            await update.message.reply_text("Tus mensajes guardados:\n" + "\n".join(mensajes))
        else:
            await update.message.reply_text("No tienes mensajes guardados aún.")
        return

    # 4. Si no es comando, responder con GPT-4o
    try:
        respuesta = openai.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": text}]
        )
        bot_reply = respuesta.choices[0].message.content
        await update.message.reply_text(bot_reply)
    except Exception as e:
        await update.message.reply_text("¡Ups! No pude responder en este momento.")
        print("Error con OpenAI:", e)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot sencillo listo y corriendo.")
    app.run_polling()

