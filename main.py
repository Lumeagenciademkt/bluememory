import os
import openai
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
import dateparser
import re
import json
from rapidfuzz import fuzz
import asyncio

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GRUPO_TELEGRAM_ID = os.getenv("GRUPO_TELEGRAM_ID")  # <-- grupo del .env

openai.api_key = OPENAI_API_KEY

if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDS_JSON)
    firebase_admin.initialize_app(cred)
db = firestore.client()

CAMPOS = ["cliente", "num_cliente", "proyecto", "modalidad", "fecha_hora", "observaciones"]
user_states = {}

CAMPO_FLEX = {
    "cliente": ["cliente"],
    "num_cliente": ["num cliente", "número cliente", "numero cliente", "número de cliente", "numero de cliente"],
    "proyecto": ["proyecto"],
    "modalidad": ["modalidad"],
    "fecha_hora": ["fecha hora", "fecha y hora", "hora", "fecha"],
    "observaciones": ["observacion", "observaciones", "observación", "observaciones", "nota", "notas"]
}

def campo_a_clave(campo_usuario):
    campo_usuario = campo_usuario.replace("_", " ").strip().lower()
    for clave, variantes in CAMPO_FLEX.items():
        if campo_usuario == clave or campo_usuario in variantes:
            return clave
        for var in variantes:
            if campo_usuario in var or var in campo_usuario:
                return clave
    return None

def campos_legibles():
    return ", ".join([
        "cliente", "num cliente", "proyecto", "modalidad", "fecha hora", "observaciones"
    ])

def prompt_gpt_neomind(texto, chat_hist=None):
    prompt = f"""
Eres un asistente que organiza, consulta y edita recordatorios. El usuario puede preguntar por fecha, cliente, proyecto, modalidad, observaciones, etc.
Tu objetivo es:
- Detectar si el usuario quiere modificar algún recordatorio existente (palabras clave como modificar, cambiar, editar, reprogramar).
- Si detectas intención de modificar, intenta extraer qué campo desea cambiar y a qué valor. Si no está claro, responde el JSON con intención "modificar" y los campos que logres extraer (incluyendo el criterio de búsqueda, como cliente o fecha, y el campo que desea cambiar).

Devuelve SOLO este JSON:

{{
  "intencion": "consultar" | "agendar" | "modificar" | "otro",
  "fecha": "",
  "busqueda": {{
    "campo": "",
    "valor": ""
  }},
  "modificar": {{
    "campo": "",
    "nuevo_valor": ""
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
- Si es por campo (“¿cuándo es mi reunión con Abelardo?”), pon "campo": "cliente" y "valor": "Abelardo".
- Si es modificar, llena los datos posibles en "busqueda" (para identificar el recordatorio) y en "modificar" (qué campo y a qué valor).
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
        "modificar": {"campo": "", "nuevo_valor": ""},
        "campos": {k: "" for k in CAMPOS}
    }

def parse_fecha_gpt(fecha_str):
    if not fecha_str:
        return None
    if fecha_str.strip().lower() in ["hoy", "ahora"]:
        return datetime.now(pytz.timezone("America/Lima")).date()
    if fecha_str.strip().lower() == "mañana":
        return (datetime.now(pytz.timezone("America/Lima") ) + timedelta(days=1)).date()
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

# ==================== NUEVAS FUNCIONES GRUPO/FORMATO BONITO =====================

def build_group_message(datos, username):
    fecha_legible = datos.get("fecha_hora", "")
    dt = parse_fecha_hora_gpt(fecha_legible)
    if dt:
        fecha_legible = dt.strftime("%Y-%m-%d %H:%M")
    return (
        f"📅 *Nueva reunión programada!*\n"
        f"👤 *Cliente:* {datos.get('cliente','')}\n"
        f"📞 *Número:* {datos.get('num_cliente','')}\n"
        f"🏗️ *Proyecto:* {datos.get('proyecto','')}\n"
        f"💡 *Modalidad:* {datos.get('modalidad','')}\n"
        f"🗓️ *Fecha y hora:* {fecha_legible}\n"
        f"📝 *Observaciones:* {datos.get('observaciones','')}\n\n"
        f"Recordatorio creado por @{username}"
    )

def build_recordatorio_resumido(datos, tipo, username):
    # tipo: "10min" o "hora"
    dt = parse_fecha_hora_gpt(datos.get("fecha_hora", ""))
    fecha_legible = dt.strftime("%d/%m/%Y %H:%M") if dt else datos.get("fecha_hora","")
    encabezado = "⏰ *Tienes una reunión en 10 minutos!*" if tipo == "10min" else "⏰ *Es hora de tu reunión!*"
    return (
        f"{encabezado}\n"
        f"👤 *Cliente:* {datos.get('cliente','')}\n"
        f"📞 *Número:* {datos.get('num_cliente','')}\n"
        f"🗓️ *Fecha y hora:* {fecha_legible}\n"
        f"📝 *Observaciones:* {datos.get('observaciones','')}\n"
        f"Creado por @{username}"
    )

# ===============================================================================

async def consulta_citas(update, context, fecha=None, campo=None, valor=None):
    chat_id = update.effective_chat.id
    user_id = chat_id

    query = db.collection("recordatorios").where("telegram_id", "==", user_id)

    if fecha:
        fecha_iso = fecha.isoformat()
        citas = query.stream()
        citas_lista = [dict(c.to_dict(), doc_id=c.id) for c in citas if c.to_dict().get("fecha_hora", "").startswith(fecha_iso)]
        msg_head = f"Recordatorios para {fecha.strftime('%d de %B de %Y')}:"
    elif campo and valor:
        citas = query.stream()
        citas_lista = [dict(c.to_dict(), doc_id=c.id) for c in citas if valor.lower() in str(c.to_dict().get(campo, "")).lower()]
        msg_head = f"Tus recordatorios por {campo.replace('_',' ')}: {valor}"
    else:
        hoy = datetime.now(pytz.timezone("America/Lima")).date().isoformat()
        citas = query.where("fecha_hora", ">=", hoy).stream()
        citas_lista = [dict(c.to_dict(), doc_id=c.id) for c in citas]
        msg_head = "Tus recordatorios pendientes:"

    return citas_lista, msg_head

async def consulta_observaciones_similar(update, context, query_text):
    chat_id = update.effective_chat.id
    user_id = chat_id
    records = db.collection("recordatorios").where("telegram_id", "==", user_id).stream()
    resultados = []
    for r in records:
        d = r.to_dict()
        obs = d.get("observaciones", "")
        score = fuzz.token_set_ratio(query_text.lower(), obs.lower())
        if score > 60:
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

async def get_chat_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_title = update.effective_chat.title or "(Privado)"
    await update.message.reply_text(f"🆔 El chat_id de este {'grupo' if update.effective_chat.type in ['group', 'supergroup'] else 'chat'} es:\n`{chat_id}`\nNombre: {chat_title}", parse_mode="Markdown")

async def mensaje_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    texto = update.message.text.strip() if update.message.text else ""
    chat_type = update.effective_chat.type
    bot_username = context.bot.username.lower()

    # --- SOLO RESPONDE EN GRUPO SI LO ETIQUETAN ---
    if chat_type in ["group", "supergroup"]:
        if f"@{bot_username}" not in texto.lower():
            return
        texto = texto.replace(f"@{bot_username}", "").strip()

    # ---- RESET/ST0P: Limpia el estado ----
    if texto.lower() in ["reset", "/reset", "stop", "/stop", "cancelar", "/cancelar"]:
        user_states[chat_id] = {}
        await update.message.reply_text("🔄 Conversación reseteada. Puedes comenzar de nuevo.")
        return

    if chat_id not in user_states:
        user_states[chat_id] = {}

    estado = user_states[chat_id].get("estado", None)

    # --- FLUJO MODIFICACIÓN (nuevo robusto) ---
    if estado == "modificar_pendiente":
        gpt_result = prompt_gpt_neomind(texto)
        campo = gpt_result.get("busqueda", {}).get("campo", "")
        valor = gpt_result.get("busqueda", {}).get("valor", "")
        fecha = parse_fecha_gpt(gpt_result.get("fecha", ""))

        # --- Ajuste: buscar por fecha si el campo incluye "fecha" ---
        if campo and "fecha" in campo.lower() and valor:
            fecha_detectada = parse_fecha_gpt(valor)
            if fecha_detectada:
                fecha = fecha_detectada
                campo = ""
                valor = ""

        if not campo and not valor and not fecha:
            await update.message.reply_text(
                "Por favor indícame el cliente o la fecha del recordatorio que deseas modificar."
            )
            return
        citas_lista, msg_head = await consulta_citas(update, context, fecha, campo, valor)
        if not citas_lista:
            await update.message.reply_text("No encontré recordatorios para modificar según tu criterio. Intenta ser más específico.")
            user_states[chat_id] = {}
            return
        if len(citas_lista) == 1:
            user_states[chat_id]["modificar_doc_id"] = citas_lista[0]["doc_id"]
            user_states[chat_id]["estado"] = "modificar_que_campo"
            await update.message.reply_text(
                f"Este es el recordatorio encontrado:\n🗓️ {citas_lista[0].get('fecha_hora','')[:16].replace('T', ' ')} - {citas_lista[0].get('cliente','')} ({citas_lista[0].get('proyecto','')})\nObs: {citas_lista[0].get('observaciones','')}\n\n¿Qué campo deseas modificar? ({campos_legibles()})"
            )
        else:
            msg = "Se encontraron varios recordatorios. Responde con el número de la lista para elegir cuál modificar:\n\n"
            for idx, c in enumerate(citas_lista, 1):
                f = c.get("fecha_hora", "")[:16].replace("T", " ")
                msg += f"{idx}. 🗓️ {f} - {c.get('cliente','')} ({c.get('proyecto','')})\nObs: {c.get('observaciones','')}\n\n"
            user_states[chat_id]["matches"] = citas_lista
            user_states[chat_id]["estado"] = "modificar_elegir"
            await update.message.reply_text(msg)
        return

    # --- MODIFICACIÓN MULTIPASO ORIGINAL ---
    if estado == "modificar_elegir":
        idx = None
        try:
            idx = int(texto.strip()) - 1
        except:
            pass
        matches = user_states[chat_id].get("matches", [])
        if idx is not None and 0 <= idx < len(matches):
            recordatorio = matches[idx]
            user_states[chat_id]["modificar_doc_id"] = recordatorio["doc_id"]
            user_states[chat_id]["estado"] = "modificar_que_campo"
            await update.message.reply_text(
                f"¿Qué campo deseas modificar? ({campos_legibles()})"
            )
            return
        else:
            await update.message.reply_text("Por favor responde con el número correspondiente al recordatorio que quieres modificar.")
            return

    if estado == "modificar_que_campo":
        campo_usuario = texto.strip().replace("_", " ").lower()
        campo_clave = campo_a_clave(campo_usuario)
        if not campo_clave or campo_clave not in CAMPOS:
            await update.message.reply_text(
                f"⚠️ Ese campo no es válido. Debe ser uno de: {campos_legibles()}."
            )
            return
        user_states[chat_id]["modificar_campo"] = campo_clave
        user_states[chat_id]["estado"] = "modificar_nuevo_valor"
        await update.message.reply_text(
            f"¿Cuál es el nuevo valor para '{campo_clave.replace('_',' ')}'?"
        )
        return

    if estado == "modificar_nuevo_valor":
        nuevo_valor = texto.strip()
        campo = user_states[chat_id]["modificar_campo"]
        doc_id = user_states[chat_id]["modificar_doc_id"]
        user_states[chat_id]["modificar_nuevo_valor"] = nuevo_valor

        if campo == "fecha_hora":
            dt = parse_fecha_hora_gpt(nuevo_valor)
            if not dt:
                await update.message.reply_text("No pude entender la nueva fecha/hora. Por favor, prueba con otro formato.")
                return
            nuevo_valor = dt.isoformat()
            user_states[chat_id]["modificar_nuevo_valor"] = nuevo_valor
            display_val = dt.strftime("%d de %B de %Y, %I:%M %p")
        else:
            display_val = nuevo_valor

        user_states[chat_id]["estado"] = "modificar_confirmar"
        await update.message.reply_text(
            f"¿Confirma que deseas modificar el campo '{campo.replace('_',' ')}' a:\n{display_val}\n\nResponde sí para confirmar."
        )
        return

    if estado == "modificar_confirmar":
        if texto.lower() in ["sí", "si", "ok", "dale", "confirmo"]:
            doc_id = user_states[chat_id]["modificar_doc_id"]
            campo = user_states[chat_id]["modificar_campo"]
            nuevo_valor = user_states[chat_id]["modificar_nuevo_valor"]
            db.collection("recordatorios").document(doc_id).update({campo: nuevo_valor})
            await update.message.reply_text("✅ ¡Recordatorio modificado correctamente!")
            user_states[chat_id] = {}
        else:
            await update.message.reply_text("Modificación cancelada.")
            user_states[chat_id] = {}
        return

    # --- FLUJO ANTERIOR (crear, consultar, buscar difuso) ---

    # Confirmación para guardar recordatorio
    if estado == "confirmar":
        if texto.lower() in ["sí", "si", "ok", "dale", "confirmo"]:
            datos = user_states[chat_id]["datos"]
            now = datetime.now(pytz.timezone("America/Lima"))
            datos["fecha_creacion"] = now.isoformat()
            datos["telegram_id"] = chat_id
            username = update.effective_user.username or update.effective_user.full_name or "usuario"
            datos["telegram_user"] = username
            db.collection("recordatorios").add(datos)
            user_states[chat_id] = {}

            # ENVÍA MENSAJE PRIVADO
            await update.message.reply_text("✅ ¡Reunión guardada! Te avisaré a la hora indicada y 10 minutos antes.")

            # ENVÍA AL GRUPO
            if GRUPO_TELEGRAM_ID:
                try:
                    resumen = build_group_message(datos, username)
                    await context.bot.send_message(chat_id=int(GRUPO_TELEGRAM_ID), text=resumen, parse_mode="Markdown")
                except Exception as e:
                    print("Error enviando al grupo:", e)
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

    if estado == "confirmar_busqueda":
        if texto.lower() in ["sí", "si", "ok", "dale", "confirmo"]:
            campo = user_states[chat_id]["busqueda"]["campo"]
            valor = user_states[chat_id]["busqueda"]["valor"]
            citas_lista, msg_head = await consulta_citas(update, context, None, campo, valor)
            if not citas_lista:
                await update.message.reply_text("No encontré recordatorios para esa búsqueda.")
                user_states[chat_id] = {}
            else:
                msg = msg_head + "\n\n"
                for idx, c in enumerate(citas_lista, 1):
                    f = c.get("fecha_hora", "")[:16].replace("T", " ")
                    msg += f"{idx}. 🗓️ {f} - {c.get('cliente','')} ({c.get('proyecto','')})\nObs: {c.get('observaciones','')}\n\n"
                await update.message.reply_text(msg)
            user_states[chat_id] = {}
            return
        else:
            await update.message.reply_text("OK, búsqueda cancelada.")
            user_states[chat_id] = {}
            return

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

    gpt_result = prompt_gpt_neomind(texto)

    # FLUJO DE MODIFICAR, ahora robusto:
    if gpt_result["intencion"] == "modificar":
        campo = gpt_result.get("busqueda", {}).get("campo", "")
        valor = gpt_result.get("busqueda", {}).get("valor", "")
        fecha = parse_fecha_gpt(gpt_result.get("fecha", ""))

        # --- Ajuste: buscar por fecha si el campo incluye "fecha" ---
        if campo and "fecha" in campo.lower() and valor:
            fecha_detectada = parse_fecha_gpt(valor)
            if fecha_detectada:
                fecha = fecha_detectada
                campo = ""
                valor = ""

        if not campo and not valor and not fecha:
            user_states[chat_id]["estado"] = "modificar_pendiente"
            await update.message.reply_text(
                "¿De qué cliente o de qué fecha es el recordatorio que deseas modificar?"
            )
            return
        citas_lista, msg_head = await consulta_citas(update, context, fecha, campo, valor)
        if not citas_lista:
            await update.message.reply_text("No encontré recordatorios para modificar según tu criterio. Intenta ser más específico.")
            return
        if len(citas_lista) == 1:
            user_states[chat_id]["modificar_doc_id"] = citas_lista[0]["doc_id"]
            user_states[chat_id]["estado"] = "modificar_que_campo"
            await update.message.reply_text(
                f"Este es el recordatorio encontrado:\n🗓️ {citas_lista[0].get('fecha_hora','')[:16].replace('T', ' ')} - {citas_lista[0].get('cliente','')} ({citas_lista[0].get('proyecto','')})\nObs: {citas_lista[0].get('observaciones','')}\n\n¿Qué campo deseas modificar? ({campos_legibles()})"
            )
        else:
            msg = "Se encontraron varios recordatorios. Responde con el número de la lista para elegir cuál modificar:\n\n"
            for idx, c in enumerate(citas_lista, 1):
                f = c.get("fecha_hora", "")[:16].replace("T", " ")
                msg += f"{idx}. 🗓️ {f} - {c.get('cliente','')} ({c.get('proyecto','')})\nObs: {c.get('observaciones','')}\n\n"
            user_states[chat_id]["matches"] = citas_lista
            user_states[chat_id]["estado"] = "modificar_elegir"
            await update.message.reply_text(msg)
        return

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
        citas_lista, msg_head = await consulta_citas(update, context, fecha, campo, valor)
        if not citas_lista:
            await update.message.reply_text("No tienes recordatorios para esa búsqueda.")
        else:
            msg = msg_head + "\n\n"
            for idx, c in enumerate(citas_lista, 1):
                f = c.get("fecha_hora", "")[:16].replace("T", " ")
                msg += f"{idx}. 🗓️ {f} - {c.get('cliente','')} ({c.get('proyecto','')})\nObs: {c.get('observaciones','')}\n\n"
            await update.message.reply_text(msg)
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

    await responder_gpt(update, texto)

# --------- SCHEDULER DE RECORDATORIOS MEJORADO ---------
async def scheduler_loop(app):
    while True:
        now = datetime.now(pytz.timezone("America/Lima"))
        hoy_str = now.strftime("%Y-%m-%d")
        docs = db.collection("recordatorios").stream()
        for doc in docs:
            d = doc.to_dict()
            chat_id = d.get("telegram_id")
            fecha_hora = d.get("fecha_hora")
            if not fecha_hora or not chat_id:
                continue
            dt = dateparser.parse(fecha_hora)
            if not dt:
                continue
            if dt.tzinfo is None:
                dt = pytz.timezone("America/Lima").localize(dt)
            if dt.strftime("%Y-%m-%d") != hoy_str:
                continue

            username = d.get("telegram_user", "") or "usuario"
            # 10 minutos antes
            if not d.get("avisado_10min") and 0 <= (dt - now).total_seconds() <= 600:
                try:
                    msg = build_recordatorio_resumido(d, "10min", username)
                    await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    # ENVÍA TAMBIÉN AL GRUPO
                    if GRUPO_TELEGRAM_ID:
                        await app.bot.send_message(chat_id=int(GRUPO_TELEGRAM_ID), text=msg, parse_mode="Markdown")
                    db.collection("recordatorios").document(doc.id).update({"avisado_10min": True})
                except Exception as e:
                    print(f"Error avisando 10min antes: {e}")

            # Exacto en la hora
            if not d.get("avisado_hora") and -60 <= (dt - now).total_seconds() <= 60:
                try:
                    msg = build_recordatorio_resumido(d, "hora", username)
                    await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                    if GRUPO_TELEGRAM_ID:
                        await app.bot.send_message(chat_id=int(GRUPO_TELEGRAM_ID), text=msg, parse_mode="Markdown")
                    db.collection("recordatorios").document(doc.id).update({"avisado_hora": True})
                except Exception as e:
                    print(f"Error avisando en la hora: {e}")
        await asyncio.sleep(60)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("getid", get_chat_id_handler))  # Para obtener el ID
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensaje_handler))
    print("Bot Neomind iniciado...")
    loop = asyncio.get_event_loop()
    loop.create_task(scheduler_loop(app))
    app.run_polling()

if __name__ == "__main__":
    main()
