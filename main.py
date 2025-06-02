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

# ====== Configuración ======
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

# ====== Google Sheets Setup ======
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_JSON, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1

memory = {}

def update_memory(user_id, user_msg, assistant_msg):
    if user_id not in memory:
        memory[user_id] = []
    memory[user_id].append({"role": "user", "content": user_msg})
    memory[user_id].append({"role": "assistant", "content": assistant_msg})
    memory[user_id] = memory[user_id][-15:]

def get_memory(user_id):
    return memory.get(user_id, [])

reminders = []

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
            # Recordatorio para pedir reporte al final del día (23:59)
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text
    user_id = str(user.id)
    username = user.username
    print(f"[{username}] {text}")

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

    # === Memoria de conversación ===
    conversation = get_memory(user_id)
    conversation_for_gpt = conversation[-14:] if conversation else []

    # ==== IA GPT ====
    system_content = (
        "Eres un asistente de CRM que agenda citas, extrae: nombre del cliente, número de cliente, proyecto, modalidad, fecha, hora y observaciones. "
        "Al guardar una cita, confirma siempre con frases como 'La cita ha sido agendada' o 'Recordatorio creado'. "
        "Antes de guardar, pide siempre cualquier observación o detalle especial para el campo OBSERVACIONES DEL RECORDATORIO. "
        "Admite fechas tipo 'hoy', 'mañana', 'pasado mañana', y formatos naturales de hora. "
        "Después de cada cita, pide reporte. Si no hay contexto de CRM, responde normalmente."
    )
    messages = [{"role": "system", "content": system_content}]
    messages += conversation_for_gpt
    messages.append({"role": "user", "content": text})

    try:
        response = openai.chat.completions.create(
            model="gpt-4-turbo",
            messages=messages
        )
        gpt_answer = response.choices[0].message.content.strip()
    except Exception as e:
        await update.message.reply_text("⚠️ Error con la IA, intenta de nuevo.")
        print("GPT ERROR:", e)
        return

    await update.message.reply_text(gpt_answer)
    update_memory(user_id, text, gpt_answer)

    # === Guardar en Sheets si GPT confirma/agendó ===
    agendar_keywords = ["agendada", "guardada", "registrada", "creada", "confirmada", "hecho", "recordatorio creado"]
    debe_guardar = any(k in gpt_answer.lower() for k in agendar_keywords)

    if debe_guardar:
        import re
        cliente = re.search(r"cliente[: ]*([^\n,]+)", gpt_answer, re.I)
        num_cliente = re.search(r"n[úu]mero de cliente[: ]*([^\n,]+)", gpt_answer, re.I)
        proyecto = re.search(r"proyecto[: ]*([^\n,]+)", gpt_answer, re.I)
        modalidad = re.search(r"modalidad[: ]*([^\n,]+)", gpt_answer, re.I)
        fecha = re.search(r"fecha[: ]*([^\n,]+)", gpt_answer, re.I)
        hora = re.search(r"hora[: ]*([^\n,]+)", gpt_answer, re.I)
        obs = re.search(r"observaci[óo]n(?:es)?[: ]*([^\n]+)", gpt_answer, re.I)

        cliente = cliente.group(1).strip() if cliente else ""
        num_cliente = num_cliente.group(1).strip() if num_cliente else ""
        proyecto = proyecto.group(1).strip() if proyecto else ""
        modalidad = modalidad.group(1).strip() if modalidad else ""
        fecha_txt = fecha.group(1).strip() if fecha else ""
        hora_txt = hora.group(1).strip() if hora else ""
        observaciones = obs.group(1).strip() if obs else ""

        # Fecha y hora juntos para columna B
        try:
            fecha_real = None
            if "hoy" in fecha_txt.lower():
                fecha_real = datetime.datetime.now()
            elif "mañana" in fecha_txt.lower():
                fecha_real = datetime.datetime.now() + datetime.timedelta(days=1)
            elif "pasado mañana" in fecha_txt.lower():
                fecha_real = datetime.datetime.now() + datetime.timedelta(days=2)
            else:
                fecha_real = dateparser.parse(fecha_txt)
            if hora_txt:
                fecha_real = fecha_real.replace(
                    hour=int(hora_txt.split(":")[0]), minute=int(hora_txt.split(":")[1]))
            fecha_hora = fecha_real.strftime("%Y-%m-%d %H:%M")
        except Exception:
            fecha_hora = f"{fecha_txt} {hora_txt}".strip()

        row = sheet.row_count + 1
        sheet.append_row([
            username,               # A: USUARIO
            fecha_hora,             # B: FECHA Y HORA
            cliente,                # C: CLIENTE
            num_cliente,            # D: NÚMERO DE CLIENTE
            proyecto,               # E: PROYECTO
            modalidad,              # F: MODALIDAD
            observaciones           # G: OBSERVACIONES DEL RECORDATORIO
        ])
        # Recordatorio en memoria
        if fecha_hora:
            try:
                dt = dateparser.parse(fecha_hora)
                rec_id = row
                reminders.append({
                    "id": rec_id,
                    "chat_id": update.effective_chat.id,
                    "cliente": cliente,
                    "modalidad": modalidad,
                    "proyecto": proyecto,
                    "datetime": dt,
                    "row": row
                })
                await update.message.reply_text(f"✅ Recordatorio guardado para {cliente} ({modalidad}) el {fecha_hora}. Fila {row}")
            except Exception as e:
                await update.message.reply_text("⚠️ Se guardó en Sheets pero no se pudo activar el recordatorio automático.")
    return

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot Lume listo y corriendo.")
    loop = asyncio.get_event_loop()
    loop.create_task(reminder_job(app))
    app.run_polling()

