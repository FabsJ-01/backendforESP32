import time
import os
import json
import customtkinter as ctk

# --- GLOBAL CONFIGURATION FOR GUI & SYSTEM ---
SERIAL_PORT = 'COM3'
BAUD_RATE = 115200
CONFIG_FILE = "config.json" 

VENDO_ID = "vendo_004" 
VENDO_NAME = "Lobby Dispenser 1"

current_water_level = 16000 
active_student_uid = None  
esp32 = None
app_instance = None  

# GLOBAL VARIABLES PARA SA ASYNCHRONOUS COIN & FLOW TRACKING
LIVE_ML_PER_PESO = 100 
coin_amount = 0
last_coin_time = time.time()
timeout_duration = 5.0  
is_coin_accumulation_mode = False
is_flow_monitoring_mode = False
ml_to_dispense = 0

vendo_ref = None  # Gagamitin ng Firebase handler

def save_config_to_local(name_id, name_public):
    global VENDO_ID, VENDO_NAME
    VENDO_ID = name_id
    VENDO_NAME = name_public
    config_data = {
        "vendo_id": name_id,
        "vendo_name": name_public
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(config_data, f, indent=4)
    print("💾 Configuration persistent data saved locally.")

def load_local_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Error reading configuration: {e}")
            return None
    return None