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

# --- Mensaje de sistema: rol de Blue ---
SYSTEM_PROMPT = {
    "role": "system",
    "content": (
        "Eres Blue, un asistente de inteligencia artificial para Telegram. "
        "Tu objetivo principal es ayudar a las personas a organizarse, recordar sus pendientes, "
        "citas y reuniones. Responde siempre de manera amigable y práctica. "
        "Puedes conversar de cualquier tema si el usuario lo desea, pero recuerda que eres experto "
        "en organización, productividad, recordatorios y agenda personal."
    )
}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    text = update.message.text

    # Envía el mensaje del usuario a GPT-4o con el rol de Blue
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            SYSTEM_PROMPT,
            {"role": "user", "content": text}
        ]
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
    print("🤖 Blue está listo para ayudarte a organizarte.")
    app.run_polling()
