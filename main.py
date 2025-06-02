import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
from dateutil import parser as dateparser
import datetime

# ===== Config Sheets =====
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_JSON, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip().lower()
    rows = sheet.get_all_records()
    
    # Mapea nombres de columnas (en mayúsculas)
    columnas = [c.strip().upper() for c in sheet.row_values(1)]
    
    # Helper para buscar citas por fecha
    def filtrar_citas_por_fecha(fecha=None, desde=None, hasta=None):
        resultados = []
        for row in rows:
            try:
                fh = row.get("FECHA Y HORA", "")
                dt = dateparser.parse(str(fh), dayfirst=False, fuzzy=True)
            except Exception:
                continue
            if fecha and dt.date() == fecha:
                resultados.append(row)
            elif desde and hasta and (desde <= dt.date() <= hasta):
                resultados.append(row)
        return resultados
    
    # CITAS DE HOY
    if "citas hoy" in msg:
        hoy = datetime.datetime.now().date()
        resultados = filtrar_citas_por_fecha(fecha=hoy)
        if resultados:
            texto = "\n\n".join([
                f"🗓 {r['FECHA Y HORA']} | Cliente: {r.get('CLIENTE','')} | Proyecto: {r.get('PROYECTO','')} | Modalidad: {r.get('MODALIDAD (CITA PRESENCIAL O CITA VIRTUAL O SOLO REAGENDAR UNA LLAMADA)','')}\nObs: {r.get('OBSERVACIONES DEL RECORDATORIO','')}"
                for r in resultados
            ])
            await update.message.reply_text(texto)
        else:
            await update.message.reply_text("No hay citas para hoy.")
        return
    
    # CITAS POR FECHA ESPECÍFICA (ej: citas 2025-06-12)
    if "citas " in msg:
        partes = msg.split()
        try:
            ix = partes.index("citas")
            fecha_txt = partes[ix+1]
            fecha = dateparser.parse(fecha_txt, dayfirst=False, fuzzy=True).date()
            resultados = filtrar_citas_por_fecha(fecha=fecha)
            if resultados:
                texto = "\n\n".join([
                    f"🗓 {r['FECHA Y HORA']} | Cliente: {r.get('CLIENTE','')} | Proyecto: {r.get('PROYECTO','')} | Modalidad: {r.get('MODALIDAD (CITA PRESENCIAL O CITA VIRTUAL O SOLO REAGENDAR UNA LLAMADA)','')}\nObs: {r.get('OBSERVACIONES DEL RECORDATORIO','')}"
                    for r in resultados
                ])
                await update.message.reply_text(texto)
            else:
                await update.message.reply_text("No hay citas para esa fecha.")
            return
        except Exception:
            pass
    
    # CLIENTES
    if "clientes" in msg:
        clientes = [row["CLIENTE"] for row in rows if row.get("CLIENTE")]
        texto = "Clientes registrados:\n- " + "\n- ".join(sorted(set(clientes)))
        await update.message.reply_text(texto)
        return
    
    # PROYECTOS
    if "proyectos" in msg:
        proyectos = [row["PROYECTO"] for row in rows if row.get("PROYECTO")]
        texto = "Proyectos registrados:\n- " + "\n- ".join(sorted(set(proyectos)))
        await update.message.reply_text(texto)
        return

    # BUSCAR POR CLIENTE
    if "buscar" in msg:
        partes = msg.split("buscar")
        if len(partes) > 1:
            nombre = partes[1].strip().lower()
            resultados = [row for row in rows if nombre in str(row.get("CLIENTE","")).lower()]
            if resultados:
                texto = "\n\n".join([
                    f"🗓 {r['FECHA Y HORA']} | Cliente: {r.get('CLIENTE','')} | Proyecto: {r.get('PROYECTO','')} | Modalidad: {r.get('MODALIDAD (CITA PRESENCIAL O CITA VIRTUAL O SOLO REAGENDAR UNA LLAMADA)','')}\nObs: {r.get('OBSERVACIONES DEL RECORDATORIO','')}"
                    for r in resultados
                ])
                await update.message.reply_text(texto)
            else:
                await update.message.reply_text("No hay citas con ese cliente.")
            return

    # Si pregunta por columnas específicas
    for col in columnas:
        if col.lower() in msg:
            valores = [str(row.get(col,"")) for row in rows if row.get(col)]
            await update.message.reply_text(f"Columna '{col}':\n- " + "\n- ".join(valores))
            return

    # AYUDA BÁSICA
    await update.message.reply_text(
        "Soy tu asistente CRM (Google Sheets base de datos).\nPuedes pedirme, por ejemplo:\n"
        "- citas hoy\n- citas 2025-06-12\n- buscar <nombre>\n- clientes\n- proyectos"
    )

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot CRM solo lectura listo.")
    app.run_polling()
