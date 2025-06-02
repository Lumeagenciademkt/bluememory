import os
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
import datetime
import asyncio
from dateutil import parser as dateparser

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

# Estado temporal para cada usuario
user_states = {}
memory = {}
reminders = []

# Lista de campos requeridos (en orden)
CAMPOS = [
    ("cliente", "¿Cuál es el nombre del cliente?"),
    ("num_cliente", "¿Cuál es el número del cliente (si aplica)?"),
    ("proyecto", "¿Sobre qué proyecto es la reunión?"),
    ("modalidad", "¿Modalidad? (presencial, virtual, reagendar llamada, etc)"),
    ("fecha_hora", "¿Fecha y hora de la reunión? (Ej: 2025-06-02 18:00)"),
    ("observaciones", "¿Alguna observación o detalle especial para este recordatorio? (puedes poner '-' si no hay)")
]

def update_memory(user_id, user_msg, assistant_msg):
    if user_id not in memory:
        memory[user_id] = []
    memory[user_id].append({"role": "user", "content": user_msg})
    memory[user_id].append({"role": "assistant", "content": assistant_msg})
    memory[user_id] = memory[user_id][-15:]

def get_memory(user_id):
    return memory.get(user_id, [])

async def reminder_job(application):
    while True:
        try:
            now = datetime.datetime.now()
            for r in list(reminders):
                remind_at = r["datetime"] - datetime.timedelta(minutes=10)
                if not r.get("notified") and now >= remind_at and now < r["datetime"]:
                    await application.bot.send_message(
                        chat_id=r["chat_id"],
                        text=f"⏰ ¡Recordatorio! En 10 minutos tienes: {r['modalidad']} con {r['cliente']} ({r['proyecto']}) a las {r['datetime'].strftime('%H:%M')}."
                    )
                    r["notified"] = True
                if not r.get("final") and now >= r["datetime"]:
                    await application.bot.send_message(
                        chat_id=r["chat_id"],
                        text=f"🚩 ¡Tienes ahora la cita: {r['modalidad']} con {r['cliente']} ({r['proyecto']})!"
                    )
                    r["final"] = True
            # Reporte al final del día (23:59)
            if now.hour == 23 and now.minute >= 55:
                for r in reminders:
                    if not r.get("reportado"):
                        await application.bot.send_message(
                            chat_id=r["chat_id"],
                            text=f"¿Qué pasó con la cita '{r['modalidad']}' con {r['cliente']} a las {r['datetime'].strftime('%H:%M')}? Responde con: reporte {r['row']} <observaciones>"
                        )
                        r["reportado"] = True
            await asyncio.sleep(60)
        except Exception as e:
            print("Error en reminder_job:", e)
            await asyncio.sleep(60)

def buscar_citas_usuario_fecha(username, fecha_consulta=None):
    rows = sheet.get_all_records()
    hoy = fecha_consulta or datetime.datetime.now().strftime("%Y-%m-%d")
    citas = []
    for idx, row in enumerate(rows, 2):
        if (row.get("USUARIO") == username and row.get("FECHA Y HORA")):
            try:
                fecha_row = dateparser.parse(str(row["FECHA Y HORA"]), dayfirst=False).strftime("%Y-%m-%d")
            except Exception:
                fecha_row = row["FECHA Y HORA"]
            if fecha_row == hoy:
                citas.append({
                    "row": idx,
                    "cliente": row.get("CLIENTE", ""),
                    "modalidad": row.get("MODALIDAD (CITA PRESENCIAL O CITA VIRTUAL O SOLO REAGENDAR UNA LLAMADA)", ""),
                    "hora": dateparser.parse(str(row["FECHA Y HORA"])).strftime("%H:%M") if row.get("FECHA Y HORA") else "",
                    "proyecto": row.get("PROYECTO", ""),
                    "observaciones": row.get("OBSERVACIONES DEL RECORDATORIO", "")
                })
    return citas

async def solicitar_dato(update, estado):
    for campo, pregunta in CAMPOS:
        if campo not in estado or not estado[campo]:
            await update.message.reply_text(pregunta)
            return False
    return True

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    username = user.username
    text = update.message.text.strip()
    if user_id not in user_states:
        user_states[user_id] = {}

    estado = user_states[user_id]

    # === Reportar resultado de cita ===
    if text.lower().startswith("reporte "):
        try:
            _, rec_id, *detalle = text.split(" ")
            detalle = " ".join(detalle)
            if rec_id.isdigit():
                row_idx = int(rec_id)
                sheet.update_cell(row_idx, 7, detalle) # Columna G: Observaciones del recordatorio
                await update.message.reply_text("📝 ¡Reporte/Observación guardada! Gracias.")
            else:
                await update.message.reply_text("ID inválido. Usa el número de fila del Sheet.")
        except Exception:
            await update.message.reply_text("❌ No se pudo registrar la observación. Usa: reporte <fila> <observaciones>")
        return

    # === Consulta de reuniones/citas del día ===
    if "reuniones" in text.lower() or "citas" in text.lower() or "pendientes" in text.lower():
        citas_hoy = buscar_citas_usuario_fecha(username)
        if citas_hoy:
            respuesta = "📅 Tus reuniones/citas de hoy:\n"
            for c in citas_hoy:
                respuesta += f"Fila {c['row']}: {c['modalidad']} con {c['cliente']} ({c['proyecto']}) a las {c['hora']} - Obs: {c['observaciones']}\n"
            await update.message.reply_text(respuesta)
        else:
            await update.message.reply_text("No tienes reuniones/citas registradas hoy.")
        return

    # === Recolecta datos uno a uno ===
    for campo, pregunta in CAMPOS:
        if campo not in estado or not estado[campo]:
            estado[campo] = text
            break

    completos = await solicitar_dato(update, estado)
    if not completos:
        return

    # Procesa el registro completo:
    cliente = estado["cliente"]
    num_cliente = estado["num_cliente"]
    proyecto = estado["proyecto"]
    modalidad = estado["modalidad"]
    fecha_hora = estado["fecha_hora"]
    observaciones = estado["observaciones"]

    try:
        dt = dateparser.parse(fecha_hora)
        fecha_hora_fmt = dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        await update.message.reply_text("❌ No entendí la fecha/hora. Por favor, usa formato 2025-06-02 18:00")
        estado["fecha_hora"] = ""
        await solicitar_dato(update, estado)
        return

    sheet.append_row([
        username,       # A: USUARIO
        fecha_hora_fmt, # B: FECHA Y HORA
        cliente,        # C: CLIENTE
        num_cliente,    # D: NÚMERO DE CLIENTE
        proyecto,       # E: PROYECTO
        modalidad,      # F: MODALIDAD
        observaciones   # G: OBSERVACIONES DEL RECORDATORIO
    ])
    await update.message.reply_text(
        f"✅ ¡Cita registrada para {cliente} ({modalidad}) el {fecha_hora_fmt}!\n"
        f"Proyecto: {proyecto}\n"
        f"Número de cliente: {num_cliente}\n"
        f"Observaciones: {observaciones}\n"
        "Recibirás recordatorios automáticos antes de la cita."
    )

    # Agrega recordatorio automático
    try:
        reminders.append({
            "id": len(reminders)+2,
            "chat_id": update.effective_chat.id,
            "cliente": cliente,
            "modalidad": modalidad,
            "proyecto": proyecto,
            "datetime": dt,
            "row": sheet.row_count
        })
    except Exception as e:
        print("No se pudo programar recordatorio automático:", e)

    user_states[user_id] = {}
    update_memory(user_id, text, f"Registro completado para {cliente}")

if _name_ == '_main_':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot Lume listo y corriendo.")
    loop = asyncio.get_event_loop()
    loop.create_task(reminder_job(app))
    app.run_polling()
