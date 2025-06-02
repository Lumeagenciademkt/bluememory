import os
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# ===== Cargar variables de entorno =====
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

openai.api_key = OPENAI_API_KEY

# ===== Google Sheets config =====
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_JSON, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1

# Lee toda la data del sheet una sola vez por mensaje recibido (puedes cambiar esto a algo más eficiente si tienes muchos mensajes o muchas filas)
def leer_sheet():
    rows = sheet.get_all_values()
    if not rows:
        return [], []
    header = rows[0]
    data = rows[1:]
    return header, data

# ===== GPT + Sheet Handler =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user = update.message.from_user
    user_id = str(user.id)

    # 1. Sheet query check (simple lógica: si la pregunta contiene "fila", "columna", "dame la fila", "qué hay en columna" etc)
    sheet_header, sheet_data = leer_sheet()
    respuesta = ""
    t = text.lower()

    # --- Buscar fila: "dame la fila 5", "muestra la fila 10"
    if "fila" in t:
        import re
        match = re.search(r"fila (\d+)", t)
        if match:
            fila_n = int(match.group(1))
            if 1 < fila_n <= len(sheet_data) + 1:
                fila = sheet_data[fila_n - 2]  # restar header y base 1
                respuesta = f"Fila {fila_n}:\n"
                for i, valor in enumerate(fila):
                    respuesta += f"{sheet_header[i]}: {valor}\n"
            else:
                respuesta = f"No existe la fila {fila_n} en el sheet."
        else:
            respuesta = "¿De qué fila quieres ver la información? Ejemplo: 'Dame la fila 4'."

    # --- Buscar columna: "dame la columna PROYECTO", "muestra la columna CLIENTE"
    elif "columna" in t:
        import re
        match = re.search(r"columna (.+)", t)
        if match:
            col_name = match.group(1).strip().upper()
            col_idx = None
            for idx, h in enumerate(sheet_header):
                if col_name in h.upper():
                    col_idx = idx
                    break
            if col_idx is not None:
                valores = [row[col_idx] for row in sheet_data]
                respuesta = f"Columna '{sheet_header[col_idx]}':\n" + "\n".join(valores)
            else:
                respuesta = f"No encontré la columna '{col_name}'."
        else:
            respuesta = "¿De qué columna quieres ver la información? Ejemplo: 'Dame la columna CLIENTE'."

    # --- Preguntas tipo resumen: "dime todos los clientes", "qué proyectos hay", etc.
    elif "clientes" in t or "proyectos" in t or "observaciones" in t:
        # Respuesta rápida multi-campo
        if "clientes" in t:
            col_idx = [i for i, h in enumerate(sheet_header) if "CLIENTE" in h.upper()]
            if col_idx:
                valores = [row[col_idx[0]] for row in sheet_data]
                respuesta = "Clientes registrados:\n" + "\n".join(valores)
        elif "proyectos" in t:
            col_idx = [i for i, h in enumerate(sheet_header) if "PROYECTO" in h.upper()]
            if col_idx:
                valores = [row[col_idx[0]] for row in sheet_data]
                respuesta = "Proyectos registrados:\n" + "\n".join(valores)
        elif "observaciones" in t:
            col_idx = [i for i, h in enumerate(sheet_header) if "OBSERVACIONES" in h.upper()]
            if col_idx:
                valores = [row[col_idx[0]] for row in sheet_data]
                respuesta = "Observaciones:\n" + "\n".join(valores)
        else:
            respuesta = ""

    # Si no es una pregunta del sheet, pásalo a GPT normal
    if not respuesta:
        # Manda a GPT como asistente normal
        system_content = (
            "Eres un asistente conversacional profesional. "
            "Si el usuario te pide datos de una hoja de cálculo de Google Sheets, dile que eres capaz de leer la hoja y puedes mostrar filas o columnas si te lo piden. "
            "Si la pregunta no es de Sheets, responde normalmente."
        )
        gpt_response = openai.chat.completions.create(
            model="gpt-4-turbo",
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": text}
            ]
        )
        respuesta = gpt_response.choices[0].message.content.strip()
    
    await update.message.reply_text(respuesta)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot Lector listo y corriendo.")
    app.run_polling()

