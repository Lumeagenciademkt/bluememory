import os
import openai
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
import re
import json

# --- Configuración ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

# --- Inicializa Firebase ---
if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# --- Estados de usuario ---
user_states = {}

# --- Campos de recordatorio ---
CAMPOS = [
    "cliente", "num_cliente", "proyecto", "modalidad",
    "fecha_hora", "motivo", "observaciones"
]

# --- Prompt para extracción de datos ---
def gpt_extract_fields(mensaje):
    prompt = f"""
Eres un asistente de agendas. Extrae los siguientes campos del mensaje (no inventes información):

- cliente
- num_cliente
- proyecto
- modalidad
- fecha_hora
- motivo
- observaciones

Ejemplo respuesta:
{{
  "cliente": "Juan Pérez",
  "num_cliente": "98798798",
  "proyecto": "Malabrigo",
  "modalidad": "presencial",
  "fecha_hora": "6 de junio 3pm",
  "motivo": "venta de lote",
  "observaciones": "revisar contrato"
}}

Mensaje del usuario:
\"{mensaje}\"

Responde solo en JSON.
"""
    respuesta = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    # Extrae JSON
    content = respuesta.choices[0].message.content
    try:
        campos = json.loads(content)
    except Exception:
        campos = {}
    return campos

# --- Normaliza fechas casuales a ISO (Lima) ---
def normaliza_fecha(texto):
    texto = texto.lower().strip()
    tz = pytz.timezone('America/Lima')
    now = datetime.now(tz)
    meses = {
        "enero": "01", "febrero": "02", "marzo": "03", "abril": "04", "mayo": "05", "junio": "06",
        "julio": "07", "agosto": "08", "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12"
    }
    # "hoy", "mañana"
    if "mañana" in texto:
        base = now + timedelta(days=1)
        texto = texto.replace("mañana", base.strftime("%Y-%m-%d"))
    if "hoy" in texto:
        texto = texto.replace("hoy", now.strftime("%Y-%m-%d"))
    # "6 de junio", "12 de mayo"
    m = re.search(r'(\d{1,2})\s*de\s*([a-záéíóúñ]+)', texto)
    if m:
        dia = m.group(1).zfill(2)
        mes = meses.get(m.group(2), "01")
        year = str(now.year)
        texto = texto.replace(m.group(0), f"{year}-{mes}-{dia}")
    # "YYYY-MM-DD", "DD/MM/YYYY"
    fmts = ["%Y-%m-%d %H:%M", "%Y-%m-%d %I%p", "%d/%m/%Y %H:%M", "%d/%m/%Y %I%p", "%Y-%m-%d", "%d/%m/%Y"]
    for fmt in fmts:
        try:
            fecha = datetime.strptime(texto, fmt)
            return fecha.strftime("%Y-%m-%d %H:%M")
        except:
            continue
    # "3pm", "14:00"
    m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', texto)
    if m:
        h = int(m.group(1))
        mnt = int(m.group(2) or 0)
        if m.group(3):
            if m.group(3) == "pm" and h < 12: h += 12
            if m.group(3) == "am" and h == 12: h = 0
        fecha = now.replace(hour=h, minute=mnt)
        return fecha.strftime("%Y-%m-%d %H:%M")
    return texto

# --- Solicita todos los campos de golpe ---
PLANTILLA_RECORDATORIO = (
    "¡Perfecto! Para guardar tu recordatorio, necesito esto "
    "(responde todo junto, en cualquier orden):\n"
    "- Cliente\n- Número de cliente\n- Proyecto\n- Modalidad (presencial/virtual)\n"
    "- Fecha y hora\n- Motivo\n- Observaciones\n\n"
    "Ejemplo: Juan Pérez, 98798798, Proyecto Malabrigo, presencial, 6 de junio 3pm, venta de lote, obs: revisar contrato"
)

# --- Presenta para verificación antes de guardar ---
def resumen_recordatorio(campos):
    resumen = "\n".join([
        f"Cliente: {campos.get('cliente', '')}",
        f"Num_cliente: {campos.get('num_cliente', '')}",
        f"Proyecto: {campos.get('proyecto', '')}",
        f"Modalidad: {campos.get('modalidad', '')}",
        f"Fecha_hora: {campos.get('fecha_hora', '')}",
        f"Motivo: {campos.get('motivo', '')}",
        f"Observaciones: {campos.get('observaciones', '')}"
    ])
    return resumen

# --- Consulta recordatorios por fecha ---
def consulta_recordatorios(user_id, fecha_buscada):
    docs = db.collection("recordatorios").where("user_id", "==", user_id).stream()
    result = []
    for doc in docs:
        data = doc.to_dict()
        # Compara solo la fecha (sin hora)
        if data.get("fecha_hora", "").startswith(fecha_buscada):
            result.append(data)
        else:
            # Permite consultar solo por día (2025-06-02)
            if fecha_buscada in data.get("fecha_hora", ""):
                result.append(data)
    return result

# --- Handler principal ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    text = update.message.text.strip()
    user_name = user.username or user.full_name or "usuario"

    estado = user_states.get(user_id, {})

    # --- Confirmación final ---
    if estado.get("confirmar"):
        if text.lower().strip() == "sí":
            campos = estado.get("campos", {})
            db.collection("recordatorios").add({
                "user_id": user_id,
                "usuario": user_name,
                **{k: campos.get(k, "") for k in CAMPOS}
            })
            await update.message.reply_text("✅ Recordatorio guardado correctamente.")
            user_states[user_id] = {}  # Limpia estado!
            return
        elif text.lower().strip() == "no":
            await update.message.reply_text("❌ Registro cancelado. Si quieres crear otro recordatorio, solo dímelo.")
            user_states[user_id] = {}
            return
        else:
            await update.message.reply_text("Responde SÍ para guardar o NO para cancelar.")
            return

    # --- Flujo de nuevo recordatorio ---
    if any(x in text.lower() for x in ["agenda", "recordar", "cita", "reunión"]):
        user_states[user_id] = {"campos": {}, "fase": "esperando_campos"}
        await update.message.reply_text(PLANTILLA_RECORDATORIO)
        return

    # --- Si espera campos del recordatorio ---
    if estado.get("fase") == "esperando_campos":
        # Usa GPT para interpretar campos, los completa con lo ya dado
        extraidos = gpt_extract_fields(text)
        campos = {**estado.get("campos", {}), **extraidos}
        # Normaliza fecha si existe
        if campos.get("fecha_hora"):
            campos["fecha_hora"] = normaliza_fecha(campos["fecha_hora"])
        # ¿Faltan campos?
        faltan = [k for k in CAMPOS if not campos.get(k)]
        if faltan:
            user_states[user_id] = {"campos": campos, "fase": "esperando_campos"}
            await update.message.reply_text(
                f"Faltan estos campos: {', '.join(faltan)}.\n"
                f"Responde solo los que faltan, en cualquier orden."
            )
            return
        # Si ya tiene todo: muestra resumen y pide confirmación
        resumen = resumen_recordatorio(campos)
        await update.message.reply_text(
            f"¡Perfecto! ¿Confirmas guardar este recordatorio?\n\n{resumen}\n\n"
            "Responde Sí para guardar o NO para cancelar."
        )
        user_states[user_id] = {"confirmar": True, "campos": campos}
        return

    # --- Consulta recordatorios por fecha ---
    if any(x in text.lower() for x in ["recordatorio", "pendiente", "reunión"]):
        # Extrae fecha buscada de texto
        tz = pytz.timezone('America/Lima')
        now = datetime.now(tz)
        if "mañana" in text.lower():
            fecha_buscada = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        elif "hoy" in text.lower():
            fecha_buscada = now.strftime("%Y-%m-%d")
        else:
            # Busca formato tipo "6 de junio", "2025-06-06", "12/06/2025"
            m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
            if m:
                fecha_buscada = m.group(1)
            else:
                # "6 de junio"
                m = re.search(r'(\d{1,2})\s*de\s*([a-záéíóúñ]+)', text.lower())
                if m:
                    dia = m.group(1).zfill(2)
                    meses = {
                        "enero": "01", "febrero": "02", "marzo": "03", "abril": "04", "mayo": "05", "junio": "06",
                        "julio": "07", "agosto": "08", "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12"
                    }
                    mes = meses.get(m.group(2), "01")
                    fecha_buscada = f"{now.year}-{mes}-{dia}"
                else:
                    fecha_buscada = now.strftime("%Y-%m-%d")
        recordatorios = consulta_recordatorios(user_id, fecha_buscada)
        if recordatorios:
            lista = [
                f"{r.get('fecha_hora','')} - {r.get('motivo','')} {r.get('cliente','')}"
                for r in recordatorios
            ]
            await update.message.reply_text("📅 Tus recordatorios para esa fecha:\n" + "\n".join(lista))
        else:
            await update.message.reply_text("No tienes recordatorios para esa fecha.")
        return

    # --- Chat normal GPT-4o ---
    system_prompt = (
        "Eres Blue, un asistente personal para organización y recordatorios en Telegram. "
        "Tu objetivo principal es ayudar al usuario a organizar su día, agendar recordatorios y citas, "
        "y responder de forma amistosa, útil y concisa. Si la pregunta es de organización, "
        "siempre ofrece ayuda proactiva, y si es otra cosa responde como un chatbot útil."
    )
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    )
    gpt_reply = response.choices[0].message.content.strip()
    await update.message.reply_text(gpt_reply)

    # Guarda chat en firestore (opcional)
    db.collection("chats").add({
        "user_id": user_id,
        "user_name": user_name,
        "mensaje": text,
        "respuesta": gpt_reply,
        "fecha": datetime.now()
    })

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Blue listo y corriendo. Optimizado para recordatorios inteligentes y respuestas GPT.")
    app.run_polling()

