import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
import firebase_admin
from firebase_admin import credentials, firestore
import openai

# === CONFIG ===
logging.basicConfig(level=logging.INFO)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_SERVICE_ACCOUNT_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# Temporalmente en memoria, puedes migrar a Redis o DB si luego quieres algo escalable
USER_SESSIONS = {}

# --- Define los campos necesarios ---
CAMPOS = [
    "FECHA Y HORA", 
    "CLIENTE", 
    "NÚMERO DE CLIENTE", 
    "PROYECTO", 
    "MODALIDAD", 
    "OBSERVACIONES DEL RECORDATORIO"
]

def gpt_extract_fields(mensaje):
    """
    Usa GPT-4 para analizar el mensaje y extraer (o detectar falta de) los campos necesarios.
    """
    prompt = f"""
Eres un asistente especializado en agendar citas para equipos de ventas y atención. Dado el siguiente mensaje, extrae los siguientes campos:

- FECHA Y HORA
- CLIENTE
- NÚMERO DE CLIENTE
- PROYECTO
- MODALIDAD (presencial, virtual, reagendar)
- OBSERVACIONES DEL RECORDATORIO

Si un campo NO está presente, escribe "FALTA".

Ejemplo de respuesta:
{{
    "FECHA Y HORA": "...",
    "CLIENTE": "...",
    "NÚMERO DE CLIENTE": "...",
    "PROYECTO": "...",
    "MODALIDAD": "...",
    "OBSERVACIONES DEL RECORDATORIO": "..."
}}

Mensaje: \"{mensaje}\"
    """

    respuesta = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": prompt}]
    )
    # Tomamos solo el contenido como texto plano
    import json
    content = respuesta.choices[0].message.content
    try:
        fields = json.loads(content)
    except Exception:
        fields = {}
    return fields

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.full_name
    mensaje = update.message.text

    # Verifica si el usuario está en medio de un flujo de agenda
    session = USER_SESSIONS.get(user_id, {})

    if session.get("pendiente"):
        # Está esperando que complete algún campo faltante
        fields = session["fields"]
        # Actualiza el campo pendiente con la nueva respuesta
        fields[session["pendiente"]] = mensaje
    else:
        # Primer mensaje: extrae los campos del mensaje
        fields = gpt_extract_fields(mensaje)

    # Verifica qué campos faltan
    campos_faltantes = [campo for campo in CAMPOS if fields.get(campo, "FALTA") == "FALTA"]

    if campos_faltantes:
        # Falta algún campo, pide el primero de la lista
        campo_faltante = campos_faltantes[0]
        USER_SESSIONS[user_id] = {
            "fields": fields,
            "pendiente": campo_faltante
        }
        await update.message.reply_text(f"Por favor, indícame el dato para **{campo_faltante}**.")
    else:
        # Todo completo: guarda en Firestore
        cita = {
            "USUARIO": user_name,
            **fields
        }
        db.collection("citas").add(cita)
        USER_SESSIONS.pop(user_id, None)
        await update.message.reply_text("✅ ¡Cita agendada exitosamente! Si quieres agendar otra, solo házmelo saber.")
        # Aquí puedes agregar también un resumen de los datos capturados.

# --- Main loop ---
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
