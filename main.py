import os
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

openai.api_key = OPENAI_API_KEY

# Configuración Google Sheets
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
# Usar la ruta donde Render monta tu secret file:
creds = ServiceAccountCredentials.from_json_keyfile_name('/etc/secrets/credentials.json', scope)
gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1  # Usa la primera hoja

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text
    print(f"[{user.username}] {text}")

    # Pregunta a OpenAI
    response = openai.chat.completions.create(
        model="gpt-4-turbo",
        messages=[{"role": "system", "content": "Eres un asistente experto en productividad y CRM. Resume y extrae los datos clave como nombre del cliente, fecha, hora, motivo, etc, de manera estructurada para Google Sheets. Si no hay información, solo responde normalmente."},
                  {"role": "user", "content": text}]
    )
    gpt_answer = response.choices[0].message.content.strip()
    await update.message.reply_text(gpt_answer)

    # Guarda el mensaje y la respuesta en Sheets (puedes extraer más campos si quieres)
    sheet.append_row([user.username, text, gpt_answer])

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot Lume listo y corriendo.")
    app.run_polling()

