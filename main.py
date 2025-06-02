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

# --- Cargar variables de entorno ---
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

# --- Estados de usuario en RAM ---
user_states = {}

# --- Campos y plantilla para recordatorios ---
CAMPOS = [
    "cliente", "num_cliente", "proyecto", "modalidad", "fecha_hora", "motivo", "observaciones"
]

def plantilla_recordatorio():
    return (
        "¡Perfecto! Para guardar tu recordatorio, necesito esto (responde todo junto, en cualquier orden):\n"
        "- Cliente\n"
        "- Número de cliente\n"
        "- Proyecto\n"
        "- Modalidad (presencial/virtual)\n"
        "- Fecha y hora\n"
        "- Motivo\n"
        "- Observaciones\n\n"
        "Ejemplo: Juan Pérez, 98798798, Proyecto Malabrigo, presencial, 6 de junio 3pm, venta de lote, obs: revisar contrato"
    )

# --- Normaliza fechas humanas a formato ISO (YYYY-MM-DD HH:MM) ---
def normaliza_fecha(texto):
    texto = texto.lower().strip()
    tz = pytz.timezone('America/Lima')
    now = datetime.now(tz)
    if "mañana" in texto:
        base = now + timedelta(days=1)
        texto = texto.replace("mañana", base.strftime("%Y-%m-%d"))
    if "hoy" in texto:
        texto = texto.replace("hoy", now.strftime("%Y-%m-%d"))
    # Detecta fechas tipo 6 de junio 3pm
    meses = {
        "enero":"01", "febrero":"02", "marzo":"03", "abril":"04", "mayo":"05", "junio":"06",
        "julio":"07", "agosto":"08", "septiembre":"09", "octubre":"10", "noviembre":"11", "diciembre":"12"
    }
    m = re.search(r'(\d{1,2})\s*de\s*(\w+)\s*(\d{1,2})?(am|pm)?', texto)
    if m:
        dia = m.group(1)
        mes = m.group(2)
        hora = m.group(3) or "00"
        ampm = m.group(4) or ""
        año = str(now.year)
        if mes in meses:
            mes_num = meses[mes]
            if ampm:
                hora_int = int(hora)
                if ampm == "pm" and hora_int < 12:
                    hora_int += 12
                if ampm == "am" and hora_int == 12:
                    hora_int = 0
                hora = f"{hora_int:02d}:00"
            else:
                hora = f"{int(hora):02d}:00"
            return f"{año}-{mes_num}-{int(dia):02d} {hora}"
    # Busca YYYY-MM-DD HH:MM o DD/MM/YYYY HH:MM
    for fmt in ["%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"]:
        try:
            fecha = datetime.strptime(texto, fmt)
            return fecha.strftime("%Y-%m-%d %H:%M")
        except: continue
    # Si solo hora: "3pm"
    m = re.match(r'(\d{1,2})(am|pm)', texto)
    if m:
        hora = int(m.group(1))
        if m.group(2) == 'pm' and hora < 12: hora += 12
        if m.group(2) == 'am' and hora == 12: hora = 0
        return now.strftime("%Y-%m-%d ") + f"{hora:02d}:00"
    return texto  # si falla, devuelve igual

# --- GPT Extraction mejorada ---
def gpt_parse_recordatorio(texto_usuario):
    prompt = f"""
Eres un asistente para organizar agendas empresariales. Un usuario te dará varios datos sobre un recordatorio, en frases, líneas, bullets o cualquier orden. Interpreta y extrae los campos, aunque estén separados por saltos de línea.

Devuelve SOLO un JSON válido con estos campos:
- cliente
- num_cliente
- proyecto
- modalidad
- fecha_hora
- motivo
- observaciones

Ejemplo de entrada válida:
'''
Juan Perez
98789898
Malabrigo
presencial
6 de junio 3pm
Venta lote
obs: revisar contrato
'''
Respuesta:
{{
  "cliente": "Juan Perez",
  "num_cliente": "98789898",
  "proyecto": "Malabrigo",
  "modalidad": "presencial",
  "fecha_hora": "6 de junio 3pm",
  "motivo": "Venta lote",
  "observaciones": "revisar contrato"
}}

Ahora interpreta y extrae del siguiente texto:
'''{texto_usuario}'''
Responde SOLO con el JSON.
"""
    resp = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    content = resp.choices[0].message.content
    try:
        campos = json.loads(content)
    except Exception:
        # Limpiar si viene con markdown o saltos
        content = content.replace("\n", "").replace("
json", "").replace("
", "")
        try:
            campos = json.loads(content)
        except Exception:
            campos = {}
    return campos

# --- Chequea campos faltantes ---
def campos_faltantes(campos):
    return [c for c in CAMPOS if not campos.get(c)]

# --- Consulta por fecha (YYYY-MM-DD) ---
def consulta_recordatorios(user_id, fecha_buscada):
    docs = db.collection("recordatorios").where("user_id", "==", user_id).stream()
    result = []
    for doc in docs:
        data = doc.to_dict()
        fecha_hora = data.get("fecha_hora", "")
        if fecha_hora.startswith(fecha_buscada):
            result.append(data)
    return result

# --- Handler principal ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    text = update.message.text.strip()
    user_name = user.username or user.full_name or "usuario"
    tz = pytz.timezone('America/Lima')
    now = datetime.now(tz)

    # 1. Estado: ¿está llenando recordatorio?
    estado = user_states.get(user_id, {})
    if estado.get("en_recordatorio"):
        # Espera todos los campos juntos (o los que faltan)
        last_campos = estado.get("campos", {})
        nuevos = gpt_parse_recordatorio(text)
        # Actualiza campos ya llenados
        for k in CAMPOS:
            if nuevos.get(k) and nuevos.get(k) != "FALTA":
                last_campos[k] = nuevos[k]
        # Normaliza fecha si ya está
        if last_campos.get("fecha_hora"):
            last_campos["fecha_hora"] = normaliza_fecha(last_campos["fecha_hora"])
        faltan = campos_faltantes(last_campos)
        if faltan:
            # Pide solo lo que falta
            await update.message.reply_text(
                f"Faltan estos campos: {', '.join(faltan)}.\nResponde solo los que faltan, en cualquier orden."
            )
            estado["campos"] = last_campos
            user_states[user_id] = estado
            return
        # TODO: verificación visual (opcional)
        resumen = "\n".join([f"{k.capitalize()}: {last_campos[k]}" for k in CAMPOS])
        await update.message.reply_text(f"¡Perfecto! ¿Confirmas guardar este recordatorio?\n\n{resumen}\n\nResponde SÍ para guardar o NO para cancelar.")
        estado["campos"] = last_campos
        estado["confirmar"] = True
        user_states[user_id] = estado
        return

    if estado.get("confirmar"):
        # Esperando confirmación del usuario
        if text.strip().lower() == "sí":
            campos = estado.get("campos", {})
            db.collection("recordatorios").add({
                "user_id": user_id,
                "usuario": user_name,
                **{k: campos.get(k, "") for k in CAMPOS},
            })
            await update.message.reply_text("✅ Recordatorio guardado. Te avisaré aquí mismo cuando sea la fecha.")
            user_states[user_id] = {}
        else:
            await update.message.reply_text("Registro cancelado. Si quieres crear otro recordatorio, solo dímelo.")
            user_states[user_id] = {}
        return

    # 2. Nueva solicitud de recordatorio
    if any(x in text.lower() for x in ["agenda", "recordar", "cita", "reunión", "recordatorio"]):
        await update.message.reply_text(plantilla_recordatorio())
        user_states[user_id] = {"en_recordatorio": True, "campos": {}}
        return

    # 3. Consulta de recordatorios por fecha (natural o ISO)
    if any(x in text.lower() for x in ["qué recordatorio", "qué tengo", "pendiente", "reunión"]) or re.search(r'\d{1,2} de \w+|\d{4}-\d{2}-\d{2}', text):
        # Intentar extraer fecha
        fecha_buscada = None
        m1 = re.search(r'(\d{1,2})\s*de\s*(\w+)', text.lower())
        m2 = re.search(r'(\d{4}-\d{2}-\d{2})', text)
        if m2:
            fecha_buscada = m2.group(1)
        elif m1:
            # Convierte a ISO
            meses = {
                "enero":"01", "febrero":"02", "marzo":"03", "abril":"04", "mayo":"05", "junio":"06",
                "julio":"07", "agosto":"08", "septiembre":"09", "octubre":"10", "noviembre":"11", "diciembre":"12"
            }
            dia = int(m1.group(1))
            mes = meses.get(m1.group(2), now.strftime("%m"))
            fecha_buscada = f"{now.year}-{mes}-{dia:02d}"
        elif "mañana" in text.lower():
            fecha_buscada = (now + timedelta(days=1)).strftime("%Y-%m-%d")
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

    # 4. Chat normal IA
    system_prompt = (
        "Eres Blue, un asistente personal de productividad en Telegram. "
        "Tu función principal es ayudar a organizar, agendar recordatorios y citas, y responder cualquier duda de forma amigable. "
        "Si la consulta es de organización o gestión, ofrece ayuda proactiva; si es general, responde como un chatbot útil."
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

    # Guarda chat en firestore
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
    print("🤖 Blue listo, con extracción flexible y confirmación de recordatorios.")
    app.run_polling()

