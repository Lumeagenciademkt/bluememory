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

# --- Configuración inicial ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

# --- Firebase ---
if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# --- Campos a solicitar ---
RECORDATORIO_CAMPOS = [
    "cliente", "num_cliente", "proyecto", "modalidad",
    "fecha_hora", "motivo", "observaciones"
]

# --- GPT extraction: separa y normaliza los campos del mensaje del usuario ---
def gpt_parse_recordatorio(texto_usuario):
    prompt = f"""
Eres un experto organizador. Extrae los siguientes campos del mensaje (pueden estar en cualquier orden):

- cliente
- num_cliente
- proyecto
- modalidad
- fecha_hora
- motivo
- observaciones

Devuelve SOLO un JSON válido con los campos. Si falta alguno, pon "FALTA".

Ejemplo:
Usuario: Juan Pérez, 98789898, proyecto Malabrigo, presencial, 6 de junio 3pm, venta de lote, obs: revisar contrato

{{"cliente": "Juan Pérez", "num_cliente": "98789898", "proyecto": "Malabrigo", "modalidad": "presencial", "fecha_hora": "6 de junio 3pm", "motivo": "venta de lote", "observaciones": "revisar contrato"}}

Ahora procesa este mensaje:
{texto_usuario}
    """
    resp = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    content = resp.choices[0].message.content
    try:
        campos = json.loads(content)
    except Exception:
        campos = {}
    return campos

# --- Normaliza y parsea fechas ---
def normaliza_fecha(texto):
    tz = pytz.timezone('America/Lima')
    now = datetime.now(tz)

    # Reemplaza 'hoy', 'mañana'
    if "mañana" in texto.lower():
        base = now + timedelta(days=1)
        texto = texto.lower().replace("mañana", base.strftime("%Y-%m-%d"))
    if "hoy" in texto.lower():
        texto = texto.lower().replace("hoy", now.strftime("%Y-%m-%d"))

    # Detección robusta de fechas comunes
    patrones = [
        (r"(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})[ ,a]*([0-9]{1,2})(?::([0-9]{2}))?\s*(am|pm)?", "%d/%m/%Y %I:%M%p"), # 06/06/2025 3:00pm
        (r"(\d{1,2}) de (\w+) de (\d{4})[ ,a]*([0-9]{1,2})(?::([0-9]{2}))?\s*(am|pm)?", None), # 6 de junio de 2025 3pm
        (r"(\d{4})-(\d{2})-(\d{2})[ ,a]*([0-9]{1,2})(?::([0-9]{2}))?\s*(am|pm)?", "%Y-%m-%d %I:%M%p"), # 2025-06-06 3:00pm
        (r"(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})", "%d/%m/%Y"), # 06/06/2025
        (r"(\d{4})-(\d{2})-(\d{2})", "%Y-%m-%d"), # 2025-06-06
    ]

    meses = {
        "enero":"01", "febrero":"02", "marzo":"03", "abril":"04", "mayo":"05", "junio":"06",
        "julio":"07", "agosto":"08", "septiembre":"09", "setiembre":"09",
        "octubre":"10", "noviembre":"11", "diciembre":"12"
    }

    # 6 de junio de 2025 3pm, 6 de junio 3pm, etc.
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)(?:\s+de\s+(\d{4}))?(?:\s+a\s+las)?\s*([0-9]{1,2})(?::([0-9]{2}))?\s*(am|pm)?", texto, re.IGNORECASE)
    if m:
        dia, mes_str, año, hora, minuto, ampm = m.groups()
        mes = meses.get(mes_str.lower(), None)
        if not año:
            año = str(now.year)
        if not mes:
            return texto
        minuto = minuto or "00"
        hora = int(hora)
        if ampm:
            if ampm.lower() == "pm" and hora < 12:
                hora += 12
            if ampm.lower() == "am" and hora == 12:
                hora = 0
        try:
            fecha_final = datetime(int(año), int(mes), int(dia), hora, int(minuto))
            return fecha_final.strftime("%Y-%m-%d %H:%M")
        except:
            return texto

    # Por patrones
    for patron, formato in patrones:
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            grupos = m.groups()
            if "de" in patron:
                # 6 de junio de 2025 3pm
                continue
            datos = [g or "00" for g in grupos]
            if formato and ("%p" in formato or "%I" in formato):
                if len(datos) >= 6:
                    dia, mes, año, hora, minuto, ampm = datos
                    hora = int(hora)
                    if ampm and ampm.lower() == "pm" and hora < 12:
                        hora += 12
                    if ampm and ampm.lower() == "am" and hora == 12:
                        hora = 0
                    fecha_final = datetime(int(año), int(mes), int(dia), hora, int(minuto))
                else:
                    dia, mes, año = datos[:3]
                    hora, minuto = datos[3:5]
                    fecha_final = datetime(int(año), int(mes), int(dia), int(hora), int(minuto))
            elif formato:
                fecha_final = datetime.strptime(" ".join(datos), formato)
            else:
                continue
            return fecha_final.strftime("%Y-%m-%d %H:%M")

    # Si solo es hora
    m = re.search(r"([0-9]{1,2})(?::([0-9]{2}))?\s*(am|pm)?", texto)
    if m:
        h, mn, ampm = m.groups()
        h = int(h)
        mn = int(mn or 0)
        if ampm:
            if ampm.lower() == "pm" and h < 12: h += 12
            if ampm.lower() == "am" and h == 12: h = 0
        return now.replace(hour=h, minute=mn).strftime("%Y-%m-%d %H:%M")

    # No pudo parsear, devuelve texto original
    return texto

# --- Consulta recordatorios por fecha (YYYY-MM-DD) ---
def consulta_recordatorios(user_id, fecha):
    docs = db.collection("recordatorios").where("user_id", "==", user_id).stream()
    lista = []
    for doc in docs:
        data = doc.to_dict()
        fh = data.get("fecha_hora", "")
        # filtra por fecha exacta (primeros 10 chars ISO)
        if fh and fh[:10] == fecha:
            desc = f"{fh} - {data.get('motivo','')} {data.get('cliente','')}"
            lista.append(desc)
    return lista

# --- Handler principal ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    user_name = user.username or user.first_name or "usuario"
    text = update.message.text.strip()

    # Paso 1: INICIAR CREACIÓN DE RECORDATORIO (si no estamos ya en proceso)
    if not context.user_data.get("esperando_formulario") and any(w in text.lower() for w in ["agenda", "recordar", "nuevo recordatorio", "cita", "reunión"]):
        context.user_data["esperando_formulario"] = True
        await update.message.reply_text(
            "¡Perfecto! Para guardar tu recordatorio, necesito esto (responde todo junto, en cualquier orden):\n"
            "- Cliente\n- Número de cliente\n- Proyecto\n- Modalidad (presencial/virtual)\n"
            "- Fecha y hora\n- Motivo\n- Observaciones\n\n"
            "Ejemplo: Juan Pérez, 98798798, Proyecto Malabrigo, presencial, 6 de junio 3pm, venta de lote, obs: revisar contrato"
        )
        return

    # Paso 2: ESPERANDO RESPUESTA DEL FORMULARIO
    if context.user_data.get("esperando_formulario"):
        campos = gpt_parse_recordatorio(text)
        # Normaliza fecha si la detecta
        if campos.get("fecha_hora") and campos["fecha_hora"] != "FALTA":
            campos["fecha_hora"] = normaliza_fecha(campos["fecha_hora"])
        # Detecta campos faltantes
        faltantes = [c for c in RECORDATORIO_CAMPOS if not campos.get(c) or campos[c] == "FALTA"]
        preview = "\n".join([f"{k.capitalize()}: {campos.get(k,'')}" for k in RECORDATORIO_CAMPOS])
        if not faltantes:
            # Listo para confirmar
            context.user_data["campos_previos"] = campos
            context.user_data["esperando_formulario"] = False
            context.user_data["esperando_confirmacion"] = True
            await update.message.reply_text(
                f"Voy a guardar este recordatorio:\n{preview}\n\n¿Está correcto? (Responde 'sí' para guardar o responde con las correcciones/faltantes)"
            )
        else:
            context.user_data["campos_previos"] = campos
            await update.message.reply_text(
                f"Faltan estos campos: {', '.join(faltantes)}\nResponde solo los que faltan, en cualquier orden."
            )
        return

    # Paso 3: ESPERANDO CONFIRMACIÓN O CORRECCIÓN
    if context.user_data.get("esperando_confirmacion"):
        if text.strip().lower() in ["sí", "si", "ok", "guardar", "conforme"]:
            campos = context.user_data.get("campos_previos", {})
            db.collection("recordatorios").add({
                "user_id": user_id,
                "usuario": user_name,
                **campos
            })
            await update.message.reply_text("✅ Recordatorio guardado y ordenado. ¡Te avisaré aquí mismo!")
            context.user_data.clear()
            return
        else:
            # El usuario puede escribir solo lo faltante o corregido
            campos = context.user_data.get("campos_previos", {})
            nuevos_campos = gpt_parse_recordatorio(text)
            campos.update({k: v for k, v in nuevos_campos.items() if v and v != "FALTA"})
            # Normaliza fecha si viene corregida
            if campos.get("fecha_hora"):
                campos["fecha_hora"] = normaliza_fecha(campos["fecha_hora"])
            preview = "\n".join([f"{k.capitalize()}: {campos.get(k,'')}" for k in RECORDATORIO_CAMPOS])
            faltantes = [c for c in RECORDATORIO_CAMPOS if not campos.get(c) or campos[c] == "FALTA"]
            if not faltantes:
                context.user_data["campos_previos"] = campos
                await update.message.reply_text(
                    f"¡Genial! Así quedaría:\n{preview}\n\n¿Está correcto? (Responde 'sí' para guardar)"
                )
            else:
                context.user_data["campos_previos"] = campos
                await update.message.reply_text(
                    f"Aún faltan: {', '.join(faltantes)}. Completa o corrige lo que falta:"
                )
            return

    # Paso 4: CONSULTA DE RECORDATORIOS POR FECHA
    if any(w in text.lower() for w in ["recordatorio", "pendiente", "reunión", "citas"]):
        tz = pytz.timezone('America/Lima')
        now = datetime.now(tz)
        fecha_buscada = None
        if "mañana" in text.lower():
            fecha_buscada = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        elif "hoy" in text.lower():
            fecha_buscada = now.strftime("%Y-%m-%d")
        else:
            # Busca fecha tipo '6 de junio', '2025-06-06', '06/06/2025', 'junio 6', etc.
            fecha_patrones = [
                (r"(\d{1,2})\s+de\s+(\w+)", "%d %B"),       # 6 de junio
                (r"(\d{4})-(\d{2})-(\d{2})", "%Y-%m-%d"),   # 2025-06-06
                (r"(\d{1,2})/(\d{1,2})/(\d{4})", "%d/%m/%Y") # 06/06/2025
            ]
            meses = {
                "enero":"01", "febrero":"02", "marzo":"03", "abril":"04", "mayo":"05", "junio":"06",
                "julio":"07", "agosto":"08", "septiembre":"09", "setiembre":"09",
                "octubre":"10", "noviembre":"11", "diciembre":"12"
            }
            for patron, fmt in fecha_patrones:
                m = re.search(patron, text, re.IGNORECASE)
                if m:
                    if fmt == "%d %B":
                        dia, mes_str = m.groups()
                        mes = meses.get(mes_str.lower())
                        if mes:
                            fecha_buscada = f"{now.year}-{mes}-{str(dia).zfill(2)}"
                            break
                    elif fmt == "%Y-%m-%d":
                        fecha_buscada = m.group(0)
                        break
                    elif fmt == "%d/%m/%Y":
                        dia, mes, año = m.groups()
                        fecha_buscada = f"{año}-{str(mes).zfill(2)}-{str(dia).zfill(2)}"
                        break
        # Si no detecta fecha, asume hoy
        if not fecha_buscada:
            fecha_buscada = now.strftime("%Y-%m-%d")
        recordatorios = consulta_recordatorios(user_id, fecha_buscada)
        if recordatorios:
            await update.message.reply_text("📅 Tus recordatorios para esa fecha:\n" + "\n".join(recordatorios))
        else:
            await update.message.reply_text("No tienes recordatorios para esa fecha.")
        return

    # MODO CHATGPT NORMAL para cualquier otra consulta
    system_prompt = (
        "Eres Blue, un asistente personal de productividad y recordatorios en Telegram, "
        "especializado en ayudar a organizar y recordar citas, pendientes, reuniones y tareas. "
        "Además, eres un chatbot IA capaz de responder cualquier pregunta o conversación general."
    )
    resp = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": system_prompt},{"role": "user", "content": text}]
    )
    gpt_reply = resp.choices[0].message.content.strip()
    await update.message.reply_text(gpt_reply)
    # Guarda chat
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
    print("🤖 Blue listo: recordatorios perfectos, consultas IA, y búsquedas por fecha al 100%.")
    app.run_polling()

