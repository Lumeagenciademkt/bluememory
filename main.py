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
import threading
import time
import asyncio

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

def normaliza_fecha(texto):
    texto = texto.lower().strip()
    tz = pytz.timezone('America/Lima')
    now = datetime.now(tz)
    if "mañana" in texto:
        base = now + timedelta(days=1)
        texto = texto.replace("mañana", base.strftime("%Y-%m-%d"))
    if "hoy" in texto:
        texto = texto.replace("hoy", now.strftime("%Y-%m-%d"))
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
    for fmt in ["%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"]:
        try:
            fecha = datetime.strptime(texto, fmt)
            return fecha.strftime("%Y-%m-%d %H:%M")
        except: continue
    m = re.match(r'(\d{1,2})(am|pm)', texto)
    if m:
        hora = int(m.group(1))
        if m.group(2) == 'pm' and hora < 12: hora += 12
        if m.group(2) == 'am' and hora == 12: hora = 0
        return now.strftime("%Y-%m-%d ") + f"{hora:02d}:00"
    return texto

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

Si algún dato no lo encuentras, deja el campo vacío "".

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
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300
    )
    content = resp.choices[0].message.content
    try:
        campos = json.loads(content)
    except Exception:
        content = content.replace("\n", "").replace("```json", "").replace("```", "")
        try:
            campos = json.loads(content)
        except Exception:
            campos = {}
    # Siempre devuelve todos los campos
    return {k: str(campos.get(k, "") or "") for k in CAMPOS}

def campos_faltantes(campos):
    return [c for c in CAMPOS if not (campos.get(c) and str(campos.get(c)).strip())]

def consulta_recordatorios(user_id, fecha_buscada):
    docs = db.collection("recordatorios").where("user_id", "==", user_id).stream()
    result = []
    for doc in docs:
        data = doc.to_dict()
        fecha_hora = data.get("fecha_hora", "")
        if fecha_hora.startswith(fecha_buscada):
            result.append(data)
    return result

async def send_notification(app, user_id, text):
    try:
        await app.bot.send_message(chat_id=int(user_id), text=text)
        db.collection("notificaciones").add({
            "user_id": user_id,
            "mensaje": text,
            "fecha_envio": datetime.now()
        })
    except Exception as e:
        print(f"[NOTIFY ERROR] {e}")

def cargar_recordatorios_futuros():
    now = datetime.now(pytz.timezone('America/Lima'))
    docs = db.collection("recordatorios").stream()
    futuros = []
    for doc in docs:
        data = doc.to_dict()
        user_id = data.get("user_id")
        fecha_str = data.get("fecha_hora")
        if not (user_id and fecha_str):
            continue
        try:
            fecha_dt = None
            for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]:
                try:
                    fecha_dt = datetime.strptime(fecha_str, fmt)
                    break
                except: continue
            if not fecha_dt:
                continue
            if fecha_dt > now:
                futuros.append((fecha_dt, user_id, data))
        except Exception as e:
            continue
    return futuros

def start_scheduler(app):
    def scheduler():
        print("[SCHEDULER] Notificaciones activas...")
        enviados = set()
        while True:
            futuros = cargar_recordatorios_futuros()
            now = datetime.now(pytz.timezone('America/Lima'))
            for fecha_dt, user_id, data in futuros:
                delta = (fecha_dt - now).total_seconds()
                clave = (user_id, data.get("fecha_hora", ""))
                if 0 <= delta < 90 and clave not in enviados:
                    resumen = "\n".join([f"{k.capitalize()}: {data.get(k,'')}" for k in CAMPOS])
                    text = f"🔔 *Recordatorio programado:*\n{resumen}"
                    asyncio.run(send_notification(app, user_id, text))
                    enviados.add(clave)
            time.sleep(30)
    thread = threading.Thread(target=scheduler, daemon=True)
    thread.start()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = str(user.id)
    text = update.message.text.strip()
    user_name = getattr(user, "username", None) or getattr(user, "full_name", None) or "usuario"
    tz = pytz.timezone('America/Lima')
    now = datetime.now(tz)

    estado = user_states.get(user_id, {})
    if estado.get("en_recordatorio"):
        last_campos = estado.get("campos", {})
        nuevos = gpt_parse_recordatorio(text)
        for k in CAMPOS:
            if nuevos.get(k) and nuevos.get(k).lower() != "falta":
                last_campos[k] = nuevos[k]
        if last_campos.get("fecha_hora"):
            last_campos["fecha_hora"] = normaliza_fecha(last_campos["fecha_hora"])
        faltan = campos_faltantes(last_campos)
        if faltan:
            await update.message.reply_text(
                f"Faltan estos campos: {', '.join(faltan)}.\nResponde solo los que faltan, en cualquier orden."
            )
            estado["campos"] = last_campos
            user_states[user_id] = estado
            return
        resumen = "\n".join([f"{k.capitalize()}: {last_campos[k]}" for k in CAMPOS])
        await update.message.reply_text(
            f"¡Perfecto! ¿Confirma guardar este recordatorio?\n\n{resumen}\n\nResponde SÍ para guardar o NO para cancelar."
        )
        estado["campos"] = last_campos
        estado["confirmar"] = True
        estado["en_recordatorio"] = False
        user_states[user_id] = estado
        return

    if estado.get("confirmar"):
        respuesta = text.strip().lower()
        if respuesta in ["sí", "si"]:
            campos = estado.get("campos", {})
            faltan = campos_faltantes(campos)
            if faltan:
                await update.message.reply_text(
                    f"❌ No se pudo guardar: faltan los campos {', '.join(faltan)}. Vuelve a intentarlo."
                )
                db.collection("logs").add({
                    "accion": "error_guardado_incompleto",
                    "user_id": user_id,
                    "campos": campos,
                    "faltan": faltan,
                    "fecha": datetime.now()
                })
                user_states[user_id] = {}
                return
            try:
                db.collection("recordatorios").add({
                    "user_id": user_id,
                    "usuario": user_name,
                    **{k: campos.get(k, "") for k in CAMPOS},
                })
                await update.message.reply_text("✅ Recordatorio guardado correctamente. Te avisaré aquí mismo cuando sea la fecha.")
                db.collection("logs").add({
                    "accion": "guardar_recordatorio",
                    "user_id": user_id,
                    "campos": campos,
                    "fecha": datetime.now()
                })
            except Exception as e:
                await update.message.reply_text(f"❌ Ocurrió un error guardando el recordatorio: {str(e)}")
                db.collection("logs").add({
                    "accion": "error_guardado_firestore",
                    "user_id": user_id,
                    "campos": campos,
                    "error": str(e),
                    "fecha": datetime.now()
                })
            user_states[user_id] = {}
            return
        elif respuesta == "no":
            await update.message.reply_text("Registro cancelado. Si quieres crear otro recordatorio, solo dímelo.")
            user_states[user_id] = {}
            return
        else:
            await update.message.reply_text("Por favor, responde SÍ para guardar o NO para cancelar.")
            return

    m = re.match(r"editar recordatorio\s+([\d/:-\s]+):?\s*(.+)?", text.lower())
    if m:
        fecha_raw = m.group(1).strip()
        nuevo_campo_valor = m.group(2) or ""
        user_recs = db.collection("recordatorios").where("user_id", "==", user_id).stream()
        fecha_norm = normaliza_fecha(fecha_raw)
        found = None
        for doc in user_recs:
            data = doc.to_dict()
            if fecha_norm in data.get("fecha_hora", ""):
                found = (doc, data)
                break
        if not found:
            await update.message.reply_text("No encontré ningún recordatorio con esa fecha/hora.")
            return
        doc, data = found
        m2 = re.match(r"(\w+):\s*(.+)", nuevo_campo_valor)
        if m2:
            campo, valor = m2.group(1).strip(), m2.group(2).strip()
            if campo in CAMPOS:
                db.collection("recordatorios").document(doc.id).update({campo: valor})
                await update.message.reply_text(f"✅ Recordatorio actualizado: {campo} = {valor}")
                db.collection("logs").add({
                    "accion": "editar_recordatorio",
                    "user_id": user_id,
                    "campo": campo,
                    "valor": valor,
                    "fecha": datetime.now()
                })
            else:
                await update.message.reply_text("Campo no válido. Solo puedes editar: " + ", ".join(CAMPOS))
        else:
            await update.message.reply_text("Formato inválido. Ejemplo de uso:\nEditar recordatorio 12/06/2025 16:00: motivo: Cierre de venta")
        return

    if any(x in text.lower() for x in ["agenda", "recordar", "cita", "reunión", "recordatorio"]):
        await update.message.reply_text(plantilla_recordatorio())
        user_states[user_id] = {"en_recordatorio": True, "campos": {}}
        return

    if any(x in text.lower() for x in ["qué recordatorio", "qué tengo", "pendiente", "reunión"]) or re.search(r'\d{1,2} de \w+|\d{4}-\d{2}-\d{2}', text):
        fecha_buscada = None
        m1 = re.search(r'(\d{1,2})\s*de\s*(\w+)', text.lower())
        m2 = re.search(r'(\d{4}-\d{2}-\d{2})', text)
        if m2:
            fecha_buscada = m2.group(1)
        elif m1:
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

    system_prompt = (
        "Eres Blue, un asistente personal de productividad en Telegram. "
        "Tu función principal es ayudar a organizar, agendar recordatorios y citas, y responder cualquier duda de forma amigable. "
        "Si la consulta es de organización o gestión, ofrece ayuda proactiva; si es general, responde como un chatbot útil."
    )
    try:
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            max_tokens=300
        )
        gpt_reply = response.choices[0].message.content.strip()
    except Exception as e:
        gpt_reply = f"Lo siento, ocurrió un error procesando tu consulta: {str(e)}"
    await update.message.reply_text(gpt_reply)
    try:
        db.collection("chats").add({
            "user_id": user_id,
            "user_name": user_name,
            "mensaje": text,
            "respuesta": gpt_reply,
            "fecha": datetime.now()
        })
    except:
        pass

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    start_scheduler(app)
    print("🤖 Blue listo y ahora sí, 100% confiable. Esperando mensajes.")
    app.run_polling()
