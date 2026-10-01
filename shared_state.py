import time
import os
import json
import threading
import customtkinter as ctk
import serial.tools.list_ports

# --- GLOBAL CONFIGURATION FOR GUI & SYSTEM ---

def find_esp32_port():
    """
    Awtomatikong hahanapin ang nakasaksak na Microcontroller (Arduino Uno man o ESP32-S3)
    sa mga available na USB ports gamit ang Vendor IDs (VID) at Device Descriptions.
    """
    ports = serial.tools.list_ports.comports()
    
    # 1. Hahanapin muna ang mga kilalang Microcontroller VIDs
    for port in ports:
        # 0x303A = ESP32-S3 / Espressif
        # 0x2341 = Arduino Official (Uno, Mega, etc.)
        # 0x1A86 = CH340 / USB Serial (Karaniwang Clone Arduino Uno)
        # 0x0403 = FTDI / USB-to-Serial
        if port.vid in [0x303A, 0x2341, 0x1A86, 0x0403]:
            print(f"✅ Nahanap ang Hardware Device ({port.description}) sa: {port.device}")
            return port.device

    # 2. Kung walang nahanap sa VID, hahanapin sa pangalan/description
    for port in ports:
        desc = port.description.upper()
        if "ARDUINO" in desc or "USB SERIAL" in desc or "CH340" in desc or "ESP32" in desc:
            print(f"✅ Nahanap sa description ({port.description}) sa: {port.device}")
            return port.device

    print("⚠️ Walang nahanap na kilalang VID/Description, sinusubukan ang fallback ports...")
    
    # 3. Fallback ports para sa Linux / Raspberry Pi at Windows
    fallback_list = ['/dev/ttyACM0', '/dev/ttyACM1', '/dev/ttyUSB0', 'COM3', 'COM4', 'COM5']
    for fallback in fallback_list:
        if os.path.exists(fallback) or fallback.startswith('COM'):
            print(f"⚠️ Gumagamit ng fallback port: {fallback}")
            return fallback

    print("❌ Walang nahanap na anumang serial port!")
    return None

SERIAL_PORT = find_esp32_port()
BAUD_RATE = 115200
CONFIG_FILE = "config.json"

VENDO_ID = "vendo_004"
VENDO_NAME = "Lobby Dispenser 1"

current_water_level = 16000
active_student_uid = None
esp32 = None
# Lock para siguraduhing IISANG thread lang ang gumagalaw sa serial port
# sa isang pagkakataon - iniiwasan ang corruption/garbled data na dulot ng
# sabay-sabay na read/write mula sa magkaibang threads.
serial_lock = threading.Lock()
app_instance = None

# GLOBAL VARIABLES PARA SA ASYNCHRONOUS COIN & FLOW TRACKING
LIVE_ML_PER_PESO = 100
# === FIXED PHYSICAL CALIBRATION CONSTANT ===
# Ito ay HINDI presyo/ratio (hindi ito babaguhin ng Admin Web) - ito ay
# totoong bilis ng pump/tubo mismo, base sa calibration test:
# 2500ms = 100mL -> MS_PER_ML = 2500 / 100 = 25.0
# I-update lang ito kung magbago ang PHYSICAL setup (bagong pump, ibang tubo, atbp.)
MS_PER_ML = 25.0
coin_amount = 0
last_coin_time = time.time()
timeout_duration = 5.0
is_coin_accumulation_mode = False
is_flow_monitoring_mode = False

# === BAGONG STATE PARA SA PAUSE/RESUME TOGGLE BUTTON ===
# Ginagamit para hindi mag-trigger ang safety timeout habang sinasadyang
# naka-pause ang user gamit ang physical button sa makina.
is_pump_paused = False
pause_started_at = 0.0
paused_time_offset = 0.0  # kabuuang oras (segundo) na ginugol sa pag-pause sa kasalukuyang session
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