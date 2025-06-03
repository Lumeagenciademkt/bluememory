import os
import openai
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
import dateparser
import re
import json
from rapidfuzz import process, fuzz

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

# Campos y versión "limpia" para mostrar sin guión bajo
CAMPOS = ["cliente", "num_cliente", "proyecto", "modalidad", "fecha_hora", "observaciones"]
CAMPOS_MAP = {
    "cliente": "cliente",
    "num cliente": "num_cliente",
    "numero de cliente": "num_cliente",
    "número de cliente": "num_cliente",
    "proyecto": "proyecto",
    "modalidad": "modalidad",
    "fecha hora": "fecha_hora",
    "fecha": "fecha_hora",
    "hora": "fecha_hora",
    "observaciones": "observaciones",
    "observacion": "observaciones",
    "observación": "observaciones",
    "obs": "observaciones"
}
# Para fallback en similitud
def campo_mas_cercano(texto):
    texto = texto.lower().replace("_", " ")
    candidates = list(CAMPOS_MAP.keys())
    result = process.extractOne(texto, candidates, scorer=fuzz.token_set_ratio)
    if result and result[1] > 70:
        return CAMPOS_MAP[result[0]]
    return None

user_states = {}

def prompt_gpt_neomind(texto):
    prompt = f"""
Detecta si el usuario quiere agendar, consultar o modificar un recordatorio. Si pide modificar, intenta extraer qué campo desea modificar y a qué valor (solo si es claro). Devuelve solo este JSON:
{{
  "intencion": "consultar" | "agendar" | "modificar" | "otro",
  "campos": {{
    "cliente": "",
    "num_cliente": "",
    "proyecto": "",
    "modalidad": "",
    "fecha_hora": "",
    "observaciones": ""
  }},
  "modificar": {{
    "campo": "",
    "valor": ""
  }}
}}
Usuario: {texto}
JSON:
"""
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0
    )
    content = response.choices[0].message.content
    match = re.search(r'\{[\s\S]+\}', content)
    if match:
        return json.loads(match.group(0))
    return {
        "intencion": "otro",
        "campos": {k: "" for k in CAMPOS},
        "modificar": {"campo": "", "valor": ""}
    }

def parse_fecha_hora_gpt(fecha_str):
    if not fecha_str:
        return None
    dt = dateparser.parse(fecha_str, languages=['es'])
    if dt and dt.tzinfo is None:
        dt = pytz.timezone("America/Lima").localize(dt)
    return dt

def build_resumen(datos):
    fecha_legible = datos.get("fecha_hora", "")
    dt = parse_fecha_hora_gpt(fecha_legible)
    if dt:
        fecha_legible = dt.strftime("%d de %B de %Y, %I:%M %p")
        datos["fecha_hora"] = dt.isoformat()
    return (
        f"Perfecto, esto es lo que entendí:\n"
        f"- Cliente: {datos.get('cliente','')}\n"
        f"- Número de cliente: {datos.get('num_cliente','')}\n"
        f"- Proyecto: {datos.get('proyecto','')}\n"
        f"- Modalidad: {datos.get('modalidad','')}\n"
        f"- Fecha y hora: {fecha_legible}\n"
        f"- Observaciones: {datos.get('observaciones','')}\n\n"
        "¿Está correcto? (Responde 'sí' para guardar, o dime qué cambiar)"
    )

async def mensaje_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    texto = update.message.text.strip().lower()

    if chat_id not in user_states:
        user_states[chat_id] = {}

    estado = user_states[chat_id].get("estado", None)

    # --- CREAR ---
    if estado == "crear_campos":
        datos = user_states[chat_id].get("datos", {k: "" for k in CAMPOS})
        partes = texto.split("\n")
        for idx, campo in enumerate(CAMPOS):
            if idx < len(partes):
                datos[campo] = partes[idx]
        if all(datos[k] for k in CAMPOS):
            resumen = build_resumen(datos)
            user_states[chat_id]["datos"] = datos
            user_states[chat_id]["estado"] = "crear_confirmar"
            await update.message.reply_text(resumen)
        else:
            msg = "Por favor, indícame:\n" + "\n".join([f"- {c.replace('_',' ')}" for c in CAMPOS])
            await update.message.reply_text(msg)
        return

    if estado == "crear_confirmar":
        if texto in ["sí", "si", "ok", "dale", "confirmo"]:
            datos = user_states[chat_id]["datos"]
            datos["fecha_creacion"] = datetime.now(pytz.timezone("America/Lima")).isoformat()
            datos["telegram_id"] = chat_id
            datos["telegram_user"] = update.effective_user.username or update.effective_user.full_name
            db.collection("recordatorios").add(datos)
            await update.message.reply_text("✅ ¡Recordatorio guardado!")
            user_states[chat_id] = {}
        else:
            await update.message.reply_text("Cancelado.")
            user_states[chat_id] = {}
        return

    # --- MODIFICAR ---
    if estado == "mod_esperar_campo":
        campo = campo_mas_cercano(texto)
        if not campo:
            await update.message.reply_text("No entendí qué campo deseas modificar. Intenta de nuevo: cliente, num cliente, proyecto, modalidad, fecha hora, observaciones")
            return
        user_states[chat_id]["mod_campo"] = campo
        user_states[chat_id]["estado"] = "mod_esperar_valor"
        await update.message.reply_text(f"¿Cuál es el nuevo valor para '{campo.replace('_',' ')}'?")
        return

    if estado == "mod_esperar_valor":
        campo = user_states[chat_id]["mod_campo"]
        doc_id = user_states[chat_id]["doc_id"]
        valor = texto
        if campo == "fecha_hora":
            dt = parse_fecha_hora_gpt(valor)
            if not dt:
                await update.message.reply_text("No pude entender la nueva fecha/hora. Prueba otro formato.")
                return
            valor = dt.isoformat()
        user_states[chat_id]["mod_valor"] = valor
        user_states[chat_id]["estado"] = "mod_confirmar"
        await update.message.reply_text(f"¿Confirma que deseas modificar el campo '{campo.replace('_',' ')}' a:\n{valor}\n\nResponde sí para confirmar.")
        return

    if estado == "mod_confirmar":
        if texto in ["sí", "si", "ok", "dale", "confirmo"]:
            doc_id = user_states[chat_id]["doc_id"]
            campo = user_states[chat_id]["mod_campo"]
            valor = user_states[chat_id]["mod_valor"]
            db.collection("recordatorios").document(doc_id).update({campo: valor})
            await update.message.reply_text("✅ ¡Recordatorio modificado correctamente!")
            user_states[chat_id] = {}
        else:
            await update.message.reply_text("Cancelado.")
            user_states[chat_id] = {}
        return

    # --- INTERPRETACIÓN GPT ---
    gpt_result = prompt_gpt_neomind(texto)

    # CREAR NUEVO
    if gpt_result["intencion"] == "agendar":
        datos = gpt_result["campos"]
        if all(datos[k] for k in CAMPOS):
            resumen = build_resumen(datos)
            user_states[chat_id]["datos"] = datos
            user_states[chat_id]["estado"] = "crear_confirmar"
            await update.message.reply_text(resumen)
        else:
            user_states[chat_id]["datos"] = datos
            user_states[chat_id]["estado"] = "crear_campos"
            await update.message.reply_text("Por favor, indícame los datos del recordatorio:\n" + "\n".join([f"- {c.replace('_',' ')}" for c in CAMPOS]))
        return

    # MODIFICAR EXISTENTE
    if gpt_result["intencion"] == "modificar":
        # Buscar el recordatorio del usuario más reciente
        docs = list(db.collection("recordatorios").where("telegram_id", "==", chat_id).order_by("fecha_creacion", direction=firestore.Query.DESCENDING).limit(1).stream())
        if not docs:
            await update.message.reply_text("No tienes recordatorios para modificar.")
            return
        doc = docs[0]
        doc_data = doc.to_dict()
        doc_id = doc.id
        resumen = (
            f"Este es el recordatorio más reciente:\n"
            f"Cliente: {doc_data.get('cliente','')}\n"
            f"Proyecto: {doc_data.get('proyecto','')}\n"
            f"Modalidad: {doc_data.get('modalidad','')}\n"
            f"Fecha/hora: {doc_data.get('fecha_hora','')}\n"
            f"Observaciones: {doc_data.get('observaciones','')}\n"
        )
        await update.message.reply_text(resumen + "\n¿Qué campo deseas modificar? (cliente, num cliente, proyecto, modalidad, fecha hora, observaciones)")
        user_states[chat_id]["doc_id"] = doc_id
        user_states[chat_id]["estado"] = "mod_esperar_campo"
        return

    # CONSULTA SIMPLE
    if gpt_result["intencion"] == "consultar":
        await update.message.reply_text("Solo consultas básicas habilitadas por ahora.")
        return

    # CUALQUIER OTRO FLUJO: fallback
    await update.message.reply_text("No puedo ayudar con esa solicitud. Puedes pedir crear, consultar o modificar recordatorios.")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_handler))
    print("Bot Neomind iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
