import os
import openai
import firebase_admin
from firebase_admin import credentials, firestore
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
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

# ===== Firebase config =====
if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

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
            await asyncio.sleep(60)
        except Exception as e:
            print("Error en reminder_job:", e)
            await asyncio.sleep(60)

def buscar_citas_usuario_fecha(user_id, fecha_consulta=None):
    hoy = fecha_consulta or datetime.datetime.now().strftime("%Y-%m-%d")
    citas = []
    citas_ref = db.collection("citas").where("user_id", "==", user_id)
    for doc in citas_ref.stream():
        row = doc.to_dict()
        try:
            fecha_row = dateparser.parse(str(row["fecha_hora"]), dayfirst=False).strftime("%Y-%m-%d")
        except Exception:
            fecha_row = row["fecha_hora"]
        if fecha_row == hoy:
            citas.append({
                "id": doc.id,
                "cliente": row.get("cliente", ""),
                "modalidad": row.get("modalidad", ""),
                "hora": dateparser.parse(str(row["fecha_hora"])).strftime("%H:%M") if row.get("fecha_hora") else "",
                "proyecto": row.get("proyecto", ""),
                "observaciones": row.get("observaciones", "")
            })
    return citas

async def solicitar_dato(update, estado):
    for campo, pregunta in CAMPOS:
        if campo not in estado or not estado[campo]:
            await update.message.reply_text(pregunta)
            return False
    return True

def gpt_extract_fields(mensaje):
    prompt = f"""
Eres un asistente para agendas empresariales. Dado el siguiente mensaje de un usuario, extrae estos campos:
- cliente
- num_cliente
- proyecto
- modalidad
- fecha_hora
- observaciones

Si un campo no está presente, escribe "FALTA".

Mensaje: \"{mensaje}\"
Responde solo con un JSON.
"""
    respuesta = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    import json
    try:
        fields = json.loads(respuesta.choices[0].message.content)
    except Exception:
        fields = {}
    return fields

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    text = update.message.text.strip()
    if user_id not in user_states:
        user_states[user_id] = {}

    estado = user_states[user_id]

    # === Reportar resultado de cita ===
    if text.lower().startswith("reporte "):
        try:
            _, cita_id, *detalle = text.split(" ")
            detalle = " ".join(detalle)
            db.collection("citas").document(cita_id).update({"observaciones": detalle})
            await update.message.reply_text("📝 ¡Reporte/Observación guardada! Gracias.")
        except Exception:
            await update.message.reply_text("❌ No se pudo registrar la observación. Usa: reporte <ID> <observaciones>")
        return

    # === Consulta de reuniones/citas del día ===
    if "reuniones" in text.lower() or "citas" in text.lower() or "pendientes" in text.lower():
        citas_hoy = buscar_citas_usuario_fecha(user_id)
        if citas_hoy:
            respuesta = "📅 Tus reuniones/citas de hoy:\n"
            for c in citas_hoy:
                respuesta += f"ID {c['id']}: {c['modalidad']} con {c['cliente']} ({c['proyecto']}) a las {c['hora']} - Obs: {c['observaciones']}\n"
            await update.message.reply_text(respuesta)
        else:
            await update.message.reply_text("No tienes reuniones/citas registradas hoy.")
        return

    # === Flujo inteligente con GPT-4 ===
    # Si no estamos completando un campo pendiente, extraemos lo que podamos del mensaje:
    if not estado or not any(estado.values()):
        fields = gpt_extract_fields(text)
        for campo, _ in CAMPOS:
            if fields.get(campo) and fields[campo] != "FALTA":
                estado[campo] = fields[campo]
            else:
                estado.setdefault(campo, "")

    # === Recolecta datos uno a uno ===
    for campo, pregunta in CAMPOS:
        if not estado[campo]:
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

    db.collection("citas").add({
        "user_id": user_id,
        "usuario": user.full_name,
        "fecha_hora": fecha_hora_fmt,
        "cliente": cliente,
        "num_cliente": num_cliente,
        "proyecto": proyecto,
        "modalidad": modalidad,
        "observaciones": observaciones
    })
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
        })
    except Exception as e:
        print("No se pudo programar recordatorio automático:", e)

    user_states[user_id] = {}
    update_memory(user_id, text, f"Registro completado para {cliente}")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot Lume listo y corriendo.")
    loop = asyncio.get_event_loop()
    loop.create_task(reminder_job(app))
    app.run_polling()

