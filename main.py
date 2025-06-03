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

# Campos reales y "visuales" (más amigable)
CAMPOS = [
    ("cliente", "cliente"),
    ("num_cliente", "num cliente"),
    ("proyecto", "proyecto"),
    ("modalidad", "modalidad"),
    ("fecha_hora", "fecha hora"),
    ("observaciones", "observaciones"),
]

CAMPOS_LIST = [c[0] for c in CAMPOS]
CAMPOS_VISUAL = [c[1] for c in CAMPOS]

user_states = {}

def prompt_gpt_neomind(texto, campo_modificar_prev=None):
    campos_para_gpt = ", ".join([c[1] for c in CAMPOS])
    prompt = f"""
Eres un asistente que organiza, consulta y edita recordatorios. El usuario puede preguntar por fecha, cliente, proyecto, modalidad, observaciones, etc.
- Si el usuario pide modificar (cambiar, editar, reprogramar, corregir, actualizar...) algún campo, interpreta aunque escriba sinónimos, errores o diminutivos (ej: 'obs', 'observacion', 'fecha', 'num cliente', etc.).
- Devuelve SOLO este JSON:

{{
  "intencion": "consultar" | "agendar" | "modificar" | "otro",
  "fecha": "",
  "busqueda": {{
    "campo": "",
    "valor": ""
  }},
  "modificar": {{
    "campo": "",    // El campo real interno: cliente, num_cliente, proyecto, modalidad, fecha_hora, observaciones
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

Ejemplo:
Usuario: "Quiero cambiar la observación, me equivoqué" → intencion: "modificar", modificar: {{campo: "observaciones"}}
Usuario: "Ponle 16 jirafas en la obs" → intencion: "modificar", modificar: {{campo: "observaciones", nuevo_valor: "16 jirafas"}}
Usuario: "Cambio el cliente por Juan" → intencion: "modificar", modificar: {{campo: "cliente", nuevo_valor: "Juan"}}
Usuario: "Cambia fecha a mañana a las 3" → intencion: "modificar", modificar: {{campo: "fecha_hora", nuevo_valor: "mañana a las 3"}}
Usuario: "Quiero saber mis citas" → intencion: "consultar"
Usuario: "Agenda cita con Juan el viernes" → intencion: "agendar"
Usuario: "¿Cuántos planetas tiene Marte?" → intencion: "otro"

Campos posibles a modificar: {campos_para_gpt}
Mensaje: {texto}
JSON:
"""
    if campo_modificar_prev:
        prompt += f"\nEl usuario ya dijo antes que quería modificar el campo '{campo_modificar_prev}'. Si lo repite, asume que es ese campo.\n"

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
        "campos": {k: "" for k in CAMPOS_LIST}
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
    resumen = "Perfecto, esto es lo que entendí:\n"
    for campo_real, campo_vis in CAMPOS:
        resumen += f"- {campo_vis.capitalize()}: {datos.get(campo_real,'')}\n"
    resumen += "\n¿Está correcto? (Responde 'sí' para guardar, o dime qué cambiar)"
    return resumen

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

async def mensaje_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    texto = update.message.text.strip()

    if chat_id not in user_states:
        user_states[chat_id] = {}

    estado = user_states[chat_id].get("estado", None)

    # --- MODIFICACIÓN MULTIPASO ---
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
                f"¿Qué campo deseas modificar? ({', '.join(CAMPOS_VISUAL)})"
            )
            return
        else:
            await update.message.reply_text("Por favor responde con el número correspondiente al recordatorio que quieres modificar.")
            return

    if estado == "modificar_que_campo":
        # Usa GPT para interpretar el campo aunque escriba mal o con sinónimos
        gpt_result = prompt_gpt_neomind(texto)
        campo_modificar = gpt_result.get("modificar", {}).get("campo", "")
        if campo_modificar not in CAMPOS_LIST:
            campos_legibles = ", ".join(CAMPOS_VISUAL)
            await update.message.reply_text(f"Campo no reconocido. Debe ser uno de: {campos_legibles}.")
            return
        user_states[chat_id]["modificar_campo"] = campo_modificar
        user_states[chat_id]["estado"] = "modificar_nuevo_valor"
        await update.message.reply_text(f"¿Cuál es el nuevo valor para '{dict(CAMPOS)[campo_modificar]}'?")
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
        campo_legible = dict(CAMPOS)[campo]
        await update.message.reply_text(
            f"¿Confirma que deseas modificar el campo '{campo_legible}' a:\n{display_val}\n\nResponde sí para confirmar."
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

    # --- RESTO DE FLUJO ---
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

    # --- NEOMIND LOGIC: NUEVO FLUJO DE MODIFICAR ---
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

    if gpt_result["intencion"] == "modificar":
        campo = gpt_result.get("busqueda", {}).get("campo", "")
        valor = gpt_result.get("busqueda", {}).get("valor", "")
        citas_lista, msg_head = await consulta_citas(update, context, None, campo, valor)
        if not citas_lista:
            await update.message.reply_text("No encontré recordatorios para modificar según tu criterio. Intenta ser más específico.")
            return
        if len(citas_lista) == 1:
            user_states[chat_id]["modificar_doc_id"] = citas_lista[0]["doc_id"]
            user_states[chat_id]["estado"] = "modificar_que_campo"
            await update.message.reply_text(
                f"Este es el recordatorio encontrado:\n🗓️ {citas_lista[0].get('fecha_hora','')[:16].replace('T', ' ')} - {citas_lista[0].get('cliente','')} ({citas_lista[0].get('proyecto','')})\nObs: {citas_lista[0].get('observaciones','')}\n\n¿Qué campo deseas modificar? ({', '.join(CAMPOS_VISUAL)})"
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

    # --- FLUJO ANTERIOR (consultar/agendar) ---
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
        if all(datos.get(k, "") for k in CAMPOS_LIST):
            resumen = build_resumen(datos)
            user_states[chat_id]["datos"] = datos
            user_states[chat_id]["estado"] = "confirmar"
            await update.message.reply_text(resumen)
            return
        else:
            faltantes = [dict(CAMPOS)[k] for k in CAMPOS_LIST if not datos.get(k, "")]
            if len(faltantes) > 1:
                msg = "Por favor, indícame los siguientes datos:\n" + "\n".join([f"- {campo.capitalize()}" for campo in faltantes])
            else:
                msg = "Por favor, indícame:\n" + "\n".join([f"- {campo.capitalize()}" for campo in faltantes])
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
