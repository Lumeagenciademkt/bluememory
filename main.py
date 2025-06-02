import os
import openai
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv

# Opcional: firebase para guardar historial de chat
import firebase_admin
from firebase_admin import credentials, firestore

# Carga variables de entorno
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

# Inicializa Firebase (opcional, puedes comentar si no quieres guardar historial)
if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    text = update.message.text

    # Envía el mensaje del usuario a GPT-4o
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": text}]
    )
    gpt_reply = response.choices[0].message.content.strip()

    # Envía respuesta a Telegram
    await update.message.reply_text(gpt_reply)

    # Guarda el mensaje y respuesta en Firestore (opcional)
    db.collection("chats").add({
        "user_id": user_id,
        "user_name": user.username,
        "mensaje": text,
        "respuesta": gpt_reply
    })

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot listo y corriendo solo como chat.")
    app.run_polling()
