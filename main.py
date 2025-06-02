import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import datetime

# ===== Cargar variables de entorno =====
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

# ===== Google Sheets config =====
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_JSON, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1

def buscar_citas_de_hoy(username=None):
    rows = sheet.get_all_records()
    hoy = datetime.datetime.now().strftime("%Y-%m-%d")
    citas = []
    for idx, row in enumerate(rows, 2):
        try:
            fecha = str(row.get("FECHA Y HORA", "")).split(" ")[0]
            if fecha == hoy and (not username or row.get("USUARIO") == username):
                citas.append(row)
        except:
            pass
    return citas

def buscar_por_cliente(cliente):
    rows = sheet.get_all_records()
    resultados = []
    for row in rows:
        if cliente.lower() in str(row.get("CLIENTE", "")).lower():
            resultados.append(row)
    return resultados

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text.strip().lower()
    username = user.username or user.first_name or "sin_usuario"
    
    # Leer todo el sheet
    if "leer" in text or "mostrar" in text:
        rows = sheet.get_all_records()
        respuesta = "Contenido del Sheet:\n"
        for idx, row in enumerate(rows, 2):
            respuesta += f"Fila {idx}: " + ", ".join([f"{k}: {v}" for k, v in row.items()]) + "\n"
        await update.message.reply_text(respuesta[:4000])  # Telegram límite
    
    # Mostrar citas de hoy
    elif "citas de hoy" in text or "hoy" in text:
        citas = buscar_citas_de_hoy(username)
        if citas:
            respuesta = "Tus citas de hoy:\n"
            for c in citas:
                respuesta += ", ".join([f"{k}: {v}" for k, v in c.items()]) + "\n"
        else:
            respuesta = "No tienes citas registradas hoy."
        await update.message.reply_text(respuesta[:4000])
    
    # Buscar por nombre de cliente
    elif text.startswith("buscar "):
        nombre = text.replace("buscar", "").strip()
        resultados = buscar_por_cliente(nombre)
        if resultados:
            respuesta = f"Resultados para '{nombre}':\n"
            for r in resultados:
                respuesta += ", ".join([f"{k}: {v}" for k, v in r.items()]) + "\n"
        else:
            respuesta = f"No se encontraron resultados para '{nombre}'."
        await update.message.reply_text(respuesta[:4000])

    else:
        await update.message.reply_text(
            "Comandos disponibles:\n"
            "- leer / mostrar: Muestra todo el sheet\n"
            "- citas de hoy: Muestra tus citas del día\n"
            "- buscar <nombre>: Busca citas por nombre de cliente\n"
        )

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot lector de Sheets listo.")
    app.run_polling()

