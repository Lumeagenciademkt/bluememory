import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv
from datetime import datetime, timedelta

# === Configuración ===
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

def get_crm_data():
    headers = sheet.row_values(1)
    rows = sheet.get_all_values()[1:]
    data = [dict(zip(headers, row)) for row in rows if any(row)]
    return data, headers

def parse_fecha(texto):
    """
    Intenta extraer una fecha o rango de fechas del texto.
    Ejemplos válidos: 'hoy', 'mañana', '2025-06-02', 'del 2025-06-01 al 2025-06-05'
    """
    texto = texto.lower()
    hoy = datetime.now().date()
    if "hoy" in texto:
        return hoy, hoy
    if "mañana" in texto:
        return hoy + timedelta(days=1), hoy + timedelta(days=1)
    # Rango
    if "al" in texto or "hasta" in texto:
        partes = texto.replace("al", "hasta").split("hasta")
        try:
            fecha_ini = datetime.strptime(partes[0].split()[-1], "%Y-%m-%d").date()
            fecha_fin = datetime.strptime(partes[1].strip().split()[0], "%Y-%m-%d").date()
            return fecha_ini, fecha_fin
        except:
            return None, None
    # Fecha específica
    for token in texto.replace(",", " ").split():
        try:
            fecha = datetime.strptime(token, "%Y-%m-%d").date()
            return fecha, fecha
        except:
            continue
    return None, None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    crm_data, headers = get_crm_data()
    respuesta = ""
    today = datetime.now().date()

    # ----------- CONSULTA POR FECHA (reconoce varias formas) -----------
    if "cita" in text or "reunion" in text or "agenda" in text or "pendiente" in text:
        fecha_ini, fecha_fin = parse_fecha(text)
        if not fecha_ini:
            # Si no encuentra fecha, busca por hoy
            fecha_ini = fecha_fin = today

        citas = []
        for row in crm_data:
            fh = row.get('FECHA Y HORA','')
            try:
                if fh:
                    fecha_fila = datetime.strptime(fh.split()[0], "%Y-%m-%d").date()
                    if fecha_ini <= fecha_fila <= fecha_fin:
                        citas.append(row)
            except: continue

        if citas:
            if fecha_ini == fecha_fin:
                respuesta += f"📋 *Citas para {fecha_ini}:*\n"
            else:
                respuesta += f"📋 *Citas del {fecha_ini} al {fecha_fin}:*\n"
            for c in citas:
                respuesta += f"- Cliente: {c.get('CLIENTE','')} | Proyecto: {c.get('PROYECTO','')} | Hora: {c.get('FECHA Y HORA','')} | Modalidad: {c.get('MODALIDAD (CITA PRESENCIAL O CITA VIRTUAL O SOLO REAGENDAR UNA LLAMADA)','')} | Obs: {c.get('OBSERVACIONES DEL RECORDATORIO','')}\n"
        else:
            respuesta = f"No hay citas encontradas para ese rango de fechas."
        await update.message.reply_text(respuesta, parse_mode="Markdown")
        return

    # ----------- CONSULTA POR COLUMNA -----------
    for header in headers:
        if header.lower() in text:
            col_data = [row[header] for row in crm_data if row[header]]
            if col_data:
                respuesta = f"Columna '{header}':\n" + "\n".join(f"- {v}" for v in col_data)
            else:
                respuesta = f"No hay datos en la columna '{header}'."
            await update.message.reply_text(respuesta)
            return

    # ----------- BUSCAR CLIENTE POR NOMBRE -----------
    if "buscar" in text:
        nombre = text.split("buscar",1)[-1].strip()
        resultados = [row for row in crm_data if nombre.lower() in row.get('CLIENTE','').lower()]
        if resultados:
            respuesta = f"Resultados para '{nombre}':\n"
            for c in resultados:
                respuesta += f"- Cliente: {c.get('CLIENTE','')} | Proyecto: {c.get('PROYECTO','')} | Hora: {c.get('FECHA Y HORA','')} | Modalidad: {c.get('MODALIDAD (CITA PRESENCIAL O CITA VIRTUAL O SOLO REAGENDAR UNA LLAMADA)','')} | Obs: {c.get('OBSERVACIONES DEL RECORDATORIO','')}\n"
        else:
            respuesta = f"No encontré clientes llamados '{nombre}'."
        await update.message.reply_text(respuesta)
        return

    # ----------- AYUDA GENERAL -----------
    await update.message.reply_text(
        "Soy tu asistente CRM (con Google Sheets como base de datos). "
        "Puedes pedirme, por ejemplo:\n"
        "- citas hoy\n"
        "- citas 2025-06-12\n"
        "- citas del 2025-06-01 al 2025-06-05\n"
        "- buscar juan\n"
        "- clientes\n"
        "- proyectos"
    )

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Bot CRM Sheets listo y corriendo.")
    app.run_polling()
