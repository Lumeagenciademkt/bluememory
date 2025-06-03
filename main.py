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
from rapidfuzz import fuzz

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
openai.api_key = OPENAI_API_KEY

if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

CAMPOS = ["cliente", "num_cliente", "proyecto", "modalidad", "fecha_hora", "observaciones"]
user_states = {}

def prompt_gpt_neomind(texto, chat_hist=None):
    prompt = f"""
Eres un asistente que organiza y consulta recordatorios de múltiples usuarios. El usuario puede preguntar por fecha, cliente, proyecto, modalidad, observaciones, etc.
Devuelve SOLO este JSON:

{{
  "intencion": "consultar" | "agendar" | "otro",
  "fecha": "",
  "busqueda": {{
    "campo": "",
    "valor": ""
  }},
  "campos": {{
    "cliente": "",
    "num_cliente": "",
    "proyecto": "",
    "modalidad": "",
    "fecha_hora": "",
    "observaciones": ""
  }}
}}
- Si la búsqueda es general (“¿qué citas tengo?”) deja campo y valor vacíos.
- Si la búsqueda es por campo (“¿cuándo es mi reunión con Abelardo?”), pon "campo": "cliente" y "valor": "Abelardo".
- Si es agendar, pon los datos en "campos".
Mensaje: {texto}
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
        "fecha": "",
        "busqueda": {"campo": "", "valor": ""},
        "campos": {k: "" for k in CAMPOS}
    }

def parse_fecha_gpt(fecha_str):
    if not fecha_str:
        return None
    if fecha_str.strip().lower() in ["hoy", "ahora"]:
        return datetime.now(pytz.timezone("America/Lima")).date()
    if fecha_str.strip().lower() == "mañana":
        return (datetime.now(pytz.timezone("America/Lima")) + timedelta(days=1)).date()
    dt = dateparser.parse(fecha_str, languages=['es'])
    if dt:
        return dt.date()
    return None

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

async def consulta_citas(update, context, fecha=None, campo=None, valor=None):
    chat_id = update.effective_chat.id
    user_id = chat_id

    query = db.collection("recordatorios").where("telegram_id", "==", user_id)

    if fecha:
        fecha_iso = fecha.isoformat()
        citas = query.stream()
        citas_lista = [c.to_dict() for c in citas if c.to_dict().get("fecha_hora", "").startswith(fecha_iso)]
        msg_head = f"Recordatorios para {fecha.strftime('%d de %B de %Y')}:"
    elif campo and valor:
        citas = query.stream()
        citas_lista = [c.to_dict() for c in citas if valor.lower() in str(c.to_dict().get(campo, "")).lower()]
        msg_head = f"Tus recordatorios por {campo.replace('_',' ')}: {valor}"
    else:
        hoy = datetime.now(pytz.timezone("America/Lima")).date().isoformat()
        citas = query.where("fecha_hora", ">=", hoy).stream()
        citas_lista = [c.to_dict() for c in citas]
        msg_head = "Tus recordatorios pendientes:"

    if not citas_lista:
        msg = f"No tienes recordatorios{' para esa búsqueda' if campo else (' para esa fecha' if fecha else ' pendientes')}."
    else:
        msg = msg_head + "\n\n"
        for c in citas_lista:
            f = c.get("fecha_hora", "")[:16].replace("T", " ")
            msg += f"🗓️ {f} - {c.get('cliente','')} ({c.get('proyecto','')})\nObs: {c.get('observaciones','')}\n\n"
    await update.message.reply_text(msg)

async def consulta_observaciones_similar(update, context, query_text):
    chat_id = update.effective_chat.id
    user_id = chat_id
    records = db.collection("recordatorios").where("telegram_id", "==", user_id).stream()
    resultados = []
    for r in records:
        d = r.to_dict()
        obs = d.get("observaciones", "")
        score = fuzz.token_set_ratio(query_text.lower(), obs.lower())
        if score > 60:  # umbral ajustable
            resultados.append((score, d))
    if not resultados:
        await update.message.reply_text("No encontré ningún recordatorio que coincida lo suficiente en las observaciones.")
        return
    resultados = sorted(resultados, key=lambda x: x[0], reverse=True)
    msg = "Resultados más similares en tus observaciones:\n\n"
    for score, c in resultados[:5]:
        f = c.get("fecha_hora", "")[:16].replace("T", " ")
        msg += f"🗓️ {f} - {c.get('cliente','')} ({c.get('proyecto','')})\nObs: {c.get('observaciones','')}\nSimilitud: {score}%\n\n"
    await update.message.reply_text(msg)

async def responder_gpt(update, texto):
    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": texto}],
        temperature=0.5
    )
    await update.message.reply_text(response.choices[0].message.content.strip())

async def mensaje_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    texto = update.message.text.strip()

    if chat_id not in user_states:
        user_states[chat_id] = {}

    estado = user_states[chat_id].get("estado", None)

    # Confirmación para guardar recordatorio
    if estado == "confirmar":
        if texto.lower() in ["sí", "si", "ok", "dale", "confirmo"]:
            datos = user_states[chat_id]["datos"]
            now = datetime.now(pytz.timezone("America/Lima"))
            datos["fecha_creacion"] = now.isoformat()
            datos["telegram_id"] = chat_id
            datos["telegram_user"] = update.effective_user.username or update.effective_user.full_name
            db.collection("recordatorios").add(datos)
            user_states[chat_id] = {}
            await update.message.reply_text("✅ ¡Recordatorio guardado! Te avisaré a la hora indicada y 10 minutos antes.")
            return
        elif texto.lower() in ["no", "cambiar", "editar", "modificar"]:
            await update.message.reply_text("OK, vuelve a escribir la información de tu recordatorio, todos los campos o sólo los que quieras cambiar.")
            user_states[chat_id]["estado"] = "pendiente"
            return
        else:
            gpt_result = prompt_gpt_neomind(texto)
            datos = gpt_result.get("campos", {})
            resumen = build_resumen(datos)
            user_states[chat_id]["datos"] = datos
            await update.message.reply_text(resumen)
            return

    # Confirmación para búsqueda por campo
    if estado == "confirmar_busqueda":
        if texto.lower() in ["sí", "si", "ok", "dale", "confirmo"]:
            campo = user_states[chat_id]["busqueda"]["campo"]
            valor = user_states[chat_id]["busqueda"]["valor"]
            await consulta_citas(update, context, None, campo, valor)
            user_states[chat_id] = {}
            return
        else:
            await update.message.reply_text("OK, búsqueda cancelada.")
            user_states[chat_id] = {}
            return

    # Confirmación para búsqueda difusa en observaciones
    if estado == "confirmar_observacion_similar":
        if texto.lower() in ["sí", "si", "ok", "dale", "confirmo"]:
            query_text = user_states[chat_id]["query_text"]
            await consulta_observaciones_similar(update, context, query_text)
            user_states[chat_id] = {}
            return
        else:
            await update.message.reply_text("OK, búsqueda cancelada.")
            user_states[chat_id] = {}
            return

    # Modo Neomind: interpretación global de la intención (via GPT-4o)
    gpt_result = prompt_gpt_neomind(texto)

    # Búsqueda difusa por observaciones si no detecta campo ni fecha pero el mensaje es descriptivo
    if (
        gpt_result["intencion"] == "consultar"
        and not gpt_result.get("busqueda", {}).get("campo", "")
        and not gpt_result.get("fecha", "")
        and len(texto.split()) > 5
    ):
        user_states[chat_id]["estado"] = "confirmar_observacion_similar"
        user_states[chat_id]["query_text"] = texto
        await update.message.reply_text(
            f"¿Quieres buscar entre las observaciones de tus recordatorios por: '{texto}'? (Responde sí para confirmar)"
        )
        return

    if gpt_result["intencion"] == "consultar":
        campo = gpt_result.get("busqueda", {}).get("campo", "")
        valor = gpt_result.get("busqueda", {}).get("valor", "")
        fecha = parse_fecha_gpt(gpt_result.get("fecha", ""))
        if campo and valor:
            user_states[chat_id]["estado"] = "confirmar_busqueda"
            user_states[chat_id]["busqueda"] = {"campo": campo, "valor": valor}
            await update.message.reply_text(f"¿Deseas buscar en tus recordatorios por {campo.replace('_',' ')}: {valor}? (Responde sí para confirmar)")
            return
        else:
            await consulta_citas(update, context, fecha)
            return

    if gpt_result["intencion"] == "agendar":
        datos = gpt_result["campos"]
        if all(datos.get(k, "") for k in CAMPOS):
            resumen = build_resumen(datos)
            user_states[chat_id]["datos"] = datos
            user_states[chat_id]["estado"] = "confirmar"
            await update.message.reply_text(resumen)
            return
        else:
            faltantes = [k for k in CAMPOS if not datos.get(k, "")]
            if len(faltantes) > 1:
                msg = "Por favor, indícame los siguientes datos:\n" + "\n".join([f"- {campo.replace('_', ' ').capitalize()}" for campo in faltantes])
            else:
                msg = "Por favor, indícame:\n" + "\n".join([f"- {campo.replace('_', ' ').capitalize()}" for campo in faltantes])
            user_states[chat_id]["datos"] = datos
            user_states[chat_id]["estado"] = "pendiente"
            await update.message.reply_text(msg)
            return

    # Si no entiende la intención, responde como ChatGPT
    await responder_gpt(update, texto)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_handler))
    print("Bot Neomind iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
