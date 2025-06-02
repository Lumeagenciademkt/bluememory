import os
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import datetime
import asyncio

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

# ===== Memoria por usuario =====
memory = {}

def update_memory(user_id, user_msg, assistant_msg):
    if user_id not in memory:
        memory[user_id] = []
    memory[user_id].append({"role": "user", "content": user_msg})
    memory[user_id].append({"role": "assistant", "content": assistant_msg})
    # Mantener solo los últimos 15 mensajes (7 turnos completos + 1 user msg extra)
    memory[user_id] = memory[user_id][-15:]

def get_memory(user_id):
    return memory.get(user_id, [])

# ===== Recordatorios en memoria (para demo, en real usaría base de datos o Sheets) =====
reminders = []

# ===== Async background job for reminders =====
async def reminder_job(application):
    while True:
        try:
            now = datetime.datetime.now()
            for idx, r in enumerate(list(reminders)):
                remind_at = r["datetime"] - datetime.timedelta(minutes=10)
                if not r.get("notified") and now >= remind_at and now < r["datetime"]:
                    await application.bot.send_message(
                        chat_id=r["chat_id"],
                        text=f"⏰ ¡Recordatorio! En 10 minutos tienes: {r['motivo']} con {r['cliente']} a las {r['datetime'].strftime('%H:%M')}."
                    )
                    r["notified"] = True
                # Cuando ya llega la hora exacta
                if not r.get("final") and now >= r["datetime"]:
                    await application.bot.send_message(
                        chat_id=r["chat_id"],
                        text=f"🚩 ¡Tienes ahora la cita/labor: {r['motivo']} con {r['cliente']}!"
                    )
                    r["final"] = True
            # Al final del día (23:59), pedir reporte
            if now.hour == 23 and now.minute >= 55:
                for r in reminders:
                    if not r.get("reportado"):
                        await application.bot.send_message(
                            chat_id=r["chat_id"],
                            text=f"¿Qué pasó con la cita/labor '{r['motivo']}' con {r['cliente']} a las {r['datetime'].strftime('%H:%M')}? Responde con: reporte {r['id']} [detalle]"
                        )
                        r["reportado"] = True
            await asyncio.sleep(60)  # Chequear cada minuto
        except Exception as e:
            print("Error en reminder_job:", e)
            await asyncio.sleep(60)

# ===== Manejo de mensajes =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text
    user_id = str(user.id)
    print(f"[{user.username}] {text}")

    # ==== Lógica para procesar si es un reporte de cita ====
    if text.lower().startswith("reporte "):
        try:
            _, rec_id, *detalle = text.split(" ")
            detalle = " ".join(detalle)
            for idx, r in enumerate(reminders):
                if str(r['id']) == rec_id and r['chat_id'] == update.effective_chat.id:
                    sheet.update_cell(r["row"], 8, detalle)  # Columna H: Observaciones
                    await update.message.reply_text(f"📝 ¡Reporte guardado! Gracias.")
                    break
        except Exception as ex:
            await update.message.reply_text("❌ No se pudo registrar el reporte. Intenta con: reporte <ID> <detalle>")
        return

    # ==== Memoria de conversación ====
    conversation = get_memory(user_id)
    conversation_for_gpt = conversation[-14:] if conversation else []

    # ==== Llama a OpenAI ====
    system_content = (
        "Eres un asistente experto en productividad y CRM. "
        "Si el usuario pide un recordatorio o cita, responde con un resumen estructurado, extrayendo: nombre del cliente, motivo, fecha, hora, y responde si deseas agendar el recordatorio. "
        "Al final de cada cita/recordatorio, pide un reporte de lo sucedido."
        "Si no hay contexto de CRM, solo responde normalmente."
    )
    messages = [{"role": "system", "content": system_content}]
    messages += conversation_for_gpt
    messages.append({"role": "user", "content": text})

    response = openai.chat.completions.create(
        model="gpt-4-turbo",
        messages=messages
    )
    gpt_answer = response.choices[0].message.content.strip()
    await update.message.reply_text(gpt_answer)
    update_memory(user_id, text, gpt_answer)

    # ==== Si el mensaje de GPT sugiere guardar un recordatorio, extráelo y guárdalo ====
    # Usa simple heurística, en producción se debe usar JSON estructurado desde GPT
    if "Recordatorio creado" in gpt_answer or "Agendado" in gpt_answer:
        # Simular extracción de campos desde respuesta de GPT
        # Aquí deberías usar un análisis de texto real o respuesta JSON desde GPT
        # Ejemplo esperado en la respuesta de GPT:
        # "Recordatorio creado para cliente Juan Pérez, motivo: reunión, fecha: 2025-06-01, hora: 20:00"
        try:
            import re
            cliente = re.search(r"cliente ([\w\sáéíóúÁÉÍÓÚüÜñÑ]+)", gpt_answer, re.I)
            motivo = re.search(r"motivo: ([\w\s]+)", gpt_answer, re.I)
            fecha = re.search(r"fecha: ([\d\-]+)", gpt_answer, re.I)
            hora = re.search(r"hora: ([\d:]+)", gpt_answer, re.I)
            cliente = cliente.group(1).strip() if cliente else ""
            motivo = motivo.group(1).strip() if motivo else ""
            fecha = fecha.group(1).strip() if fecha else ""
            hora = hora.group(1).strip() if hora else ""
            # Guardar en Sheets en columnas separadas
            row = sheet.row_count + 1
            sheet.append_row([
                user.username, text, gpt_answer, cliente, motivo, fecha, hora, ""  # Observaciones vacío
            ])
            # Guardar recordatorio en memoria para notificaciones
            if fecha and hora:
                dt = datetime.datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M")
                rec_id = len(reminders) + 1
                reminders.append({
                    "id": rec_id,
                    "chat_id": update.effective_chat.id,
                    "cliente": cliente,
                    "motivo": motivo,
                    "datetime": dt,
                    "row": row
                })
                await update.message.reply_text(f"✅ Recordatorio guardado para {cliente} ({motivo}) el {fecha} a las {hora}. ID: {rec_id}")
        except Exception as e:
            await update.message.reply_text("⚠️ No pude guardar el recordatorio por un error de formato.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot Lume listo y corriendo.")

    # Correr el background reminder job en paralelo
    loop = asyncio.get_event_loop()
    loop.create_task(reminder_job(app))
    app.run_polling()
