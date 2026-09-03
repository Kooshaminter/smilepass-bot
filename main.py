# main.py - Smilepass Bot for Render.com
import os
import re
import ast
import json
import time
import threading
import requests
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------- CONFIG (pulled from Render Environment Variables) ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise Exception("BOT_TOKEN environment variable not set!")

SMILEPASS_TOKEN = os.environ.get("SMILEPASS_TOKEN")
if not SMILEPASS_TOKEN:
    raise Exception("SMILEPASS_TOKEN environment variable not set!")

OFFICE_ID = int(os.environ.get("OFFICE_ID", 88))
BASE_URL = "https://api.smilepass.com/clinics"

# ---------- FLASK (to keep Render happy and handle web pings) ----------
app = Flask(__name__)

@app.route('/')
def home():
    return "Smilepass Bot is running!"

# ---------- CORE SMILEPASS FUNCTIONS (copied from your desktop app) ----------
def parse_data_text(text):
    start_match = re.search(r"DATA\s*=\s*\{", text)
    if not start_match:
        raise ValueError("Couldn't find DATA = { ... } block.")
    brace_start = text.index("{", start_match.start())
    depth = 0
    end_idx = None
    for i in range(brace_start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break
    if end_idx is None:
        raise ValueError("DATA block braces don't match.")
    dict_text = text[brace_start:end_idx]
    data = ast.literal_eval(dict_text)
    remainder = text[end_idx:]
    for key, val in re.findall(r'DATA\s*\[\s*["\'](\w+)["\']\s*\]\s*=\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')', remainder):
        try:
            data[key] = ast.literal_eval(val)
        except Exception:
            pass
    return data

def search_patient(token, name):
    headers = {"Authorization": f"Token {token}"}
    resp = requests.get(f"{BASE_URL}/patient-list/", headers=headers,
                        params={"page": 1, "office_id": OFFICE_ID, "name": name})
    resp.raise_for_status()
    return resp.json().get("results", [])

def get_policies(token, patient_id):
    headers = {"Authorization": f"Token {token}"}
    resp = requests.get(f"{BASE_URL}/patients/insurance-policies/", headers=headers,
                        params={"patient_id": patient_id})
    resp.raise_for_status()
    return resp.json()

def update_policy_fields(token, policy_id, fee_guide, deductible, specialist_fee_covered):
    payload = {}
    if fee_guide is not None:
        payload["fee_guide"] = str(fee_guide)
    if deductible is not None:
        payload["deductible"] = str(deductible)
    if specialist_fee_covered is not None:
        payload["specialist_fee_covered"] = bool(specialist_fee_covered)
    if not payload:
        return
    headers = {"Authorization": f"Token {token}", "Content-Type": "application/json"}
    resp = requests.patch(f"{BASE_URL}/patients/update-insurance-policy/?policy_id={policy_id}",
                          headers=headers, json=payload)
    resp.raise_for_status()

def create_breakdown(token, patient_id, policy_id):
    headers = {"Authorization": f"Token {token}", "Content-Type": "application/json"}
    payload = {"patient_id": patient_id, "policy_id": policy_id, "office_id": str(OFFICE_ID)}
    resp = requests.post(f"{BASE_URL}/breakdown-manual-create/", headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()["id"]

def update_plan_boxes(token, breakdown_id, plans):
    if not plans:
        return
    category_overrides = {}
    for cat in ["preventative", "basic", "major", "orthodontics"]:
        vals = plans.get(cat)
        if vals is None:
            category_overrides[cat] = {"overall_max": "0", "percentage": "0", "benefits_remaining": "0"}
        else:
            raw_max = vals.get("overall_max")
            if raw_max in (None, "", "0", 0):
                max_str = "0"
            elif str(raw_max).strip().lower() == "unlimited" or (isinstance(raw_max, (int, float)) and raw_max >= 9000000):
                max_str = "9999999.99"
            else:
                max_str = str(raw_max)
            category_overrides[cat] = {
                "overall_max": max_str,
                "percentage": str(vals.get("percentage", 0)),
                "benefits_remaining": str(vals.get("benefits_remaining", 0))
            }
    headers = {"Authorization": f"Token {token}", "Content-Type": "application/json"}
    resp = requests.patch(f"{BASE_URL}/breakdown-patient-rud/{breakdown_id}/?office_id={OFFICE_ID}",
                          headers=headers, json={"category_overrides": category_overrides})
    resp.raise_for_status()

def get_procedure_mapping(token, breakdown_id):
    headers = {"Authorization": f"Token {token}"}
    resp = requests.get(f"{BASE_URL}/breakdown-patient-details/?breakdown_id={breakdown_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    mapping = {}
    for entry in data.get("patient_procedure_coverages", []):
        code = entry["procedure_code_details"]["procedure_code"]
        mapping[code] = {
            "row_id": entry["id"],
            "procedure_internal_id": entry["procedure_code_details"]["id"],
        }
    return mapping

def save_procedure(token, breakdown_id, mapping, proc):
    code = proc["code"]
    if code not in mapping:
        raise RuntimeError(f"Procedure {code} not found.")
    info = mapping[code]
    freq = proc.get("frequency")
    occurrences = int(round(freq[0])) if freq and freq[0] is not None else None
    months = int(round(freq[1])) if freq and freq[1] is not None else None
    payload = {
        "id": info["row_id"],
        "procedure_code": info["procedure_internal_id"],
        "percentage_covered": str(proc.get("coverage", 0)) if proc.get("coverage") is not None else None,
        "overall_max": None,
        "max_occurrences": occurrences,
        "coverage_duration_months": months,
        "notes": proc.get("note") or "",
    }
    headers = {"Authorization": f"Token {token}", "Content-Type": "application/json"}
    resp = requests.patch(f"{BASE_URL}/patient-procedure-coverage-crud/?office_id={OFFICE_ID}&breakdown_id={breakdown_id}",
                          headers=headers, json=payload)
    resp.raise_for_status()

def mark_verified(token, patient_id, policy_id, note):
    headers = {"Authorization": f"Token {token}", "Content-Type": "application/json"}
    payload = {"patient_id": patient_id, "policy_id": policy_id, "request_type": "breakdown", "note": note or ""}
    resp = requests.post(f"{BASE_URL}/insurance-verification-requests/fulfill/",
                         headers=headers, params={"office_id": OFFICE_ID}, json=payload)
    resp.raise_for_status()

# ---------- PROCESSING FUNCTION ----------
def process_data(raw_text):
    try:
        data = parse_data_text(raw_text)
        patient_name = data.get('patient_name')
        if not patient_name:
            return "❌ Missing 'patient_name' in DATA. Add it."

        matches = search_patient(SMILEPASS_TOKEN, patient_name)
        if not matches:
            return f"❌ No patient found for '{patient_name}'."
        
        patient = matches[0]  # picks the first if multiple
        full_name = f"{patient['first_name']} {patient['last_name']}"
        patient_id = patient['id']

        policies = get_policies(SMILEPASS_TOKEN, patient_id)
        if not policies:
            return f"❌ {full_name} has no policies."
        policy = policies[0]
        policy_id = policy['id']

        warnings = []

        # 1. Policy fields
        try:
            update_policy_fields(SMILEPASS_TOKEN, policy_id,
                                 data.get('fee_guide'),
                                 data.get('deductible'),
                                 data.get('specialist_fee_covered'))
        except Exception as e:
            warnings.append(f"Policy fields: {e}")

        # 2. Create breakdown
        breakdown_id = create_breakdown(SMILEPASS_TOKEN, patient_id, policy_id)

        # 3. Plans
        try:
            update_plan_boxes(SMILEPASS_TOKEN, breakdown_id, data.get('plans'))
        except Exception as e:
            warnings.append(f"Plans: {e}")

        # 4. Procedures
        mapping = get_procedure_mapping(SMILEPASS_TOKEN, breakdown_id)
        procedures = data.get('procedures', [])
        seen = set()
        for proc in procedures:
            code = proc.get('code')
            if not code or code in seen:
                continue
            seen.add(code)
            try:
                save_procedure(SMILEPASS_TOKEN, breakdown_id, mapping, proc)
            except Exception as e:
                warnings.append(f"Procedure {code}: {e}")

        # 5. Mark Verified
        note = data.get('verification_note')
        if note:
            try:
                mark_verified(SMILEPASS_TOKEN, patient_id, policy_id, note)
            except Exception as e:
                warnings.append(f"Mark Verified: {e}")

        result = f"✅ SUCCESS for {full_name}\nBreakdown ID: {breakdown_id}"
        if warnings:
            result += "\n⚠️ Warnings:\n- " + "\n- ".join(warnings)
        return result

    except Exception as e:
        return f"❌ Error: {e}"

# ---------- TELEGRAM BOT HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Smilepass Bot is ready!\n"
        "Just paste your DeepSeek DATA block here.\n"
        "It will auto-fill the breakdown for the patient."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text
    chat_id = update.effective_chat.id

    # Acknowledge immediately
    await update.message.reply_text("⏳ Processing... (may take 10-15s if waking up)")

    # Run the heavy job in a separate thread to not block the bot
    def run_and_reply():
        result = process_data(raw_text)
        context.bot.send_message(chat_id=chat_id, text=result)

    threading.Thread(target=run_and_reply).start()

# ---------- START BOT AND FLASK ----------
def run_bot():
    # Create the Application
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot is polling...")
    application.run_polling()

if __name__ == "__main__":
    # Run Flask in a separate thread so Render keeps the service alive
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))).start()
    # Run the bot in the main thread
    run_bot()
