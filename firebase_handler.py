import os
import firebase_admin
from firebase_admin import credentials, db
import time
import threading
import sys
import shared_state

MAX_WATER_CAPACITY = 20000  # 20 Liters = 20,000 mL
STANDARD_RECORD_RATE = 100.0  # Fixed Standard: 100 mL per ₱1 para sa App/Firebase Log

# 🎯 DYNAMIC ABSOLUTE PATH SETUP PARA SA KEY.JSON
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CRED_PATH = os.path.join(BASE_DIR, "key.json")

# ==============================================================================
# ⏱️ SECONDS & TIME CALCULATION FUNCTION (PYTHON MASTER TIME CONTROLLER)
# ==============================================================================
def calculate_dispense_seconds(pesos):
    """
    Kina-calculate ang eksaktong seconds base sa hulog na peso.
    May multiplier/calibration factor para accurate ang lalabas na tubig.
    """
    # Baseline seconds bawat 1 peso (100mL baseline = 4.10 seconds)
    base_seconds_per_peso = 4.10  
    
    # Dynamic Admin Rate (halimbawa: 150 mL per peso calibration)
    admin_ml_rate = float(getattr(shared_state, 'LIVE_ML_PER_PESO', 100.0))
    volume_ratio = admin_ml_rate / 100.0  # e.g., 150 / 100 = 1.5

    # Calibration Factor base sa dami ng peso
    factor = 1.00
    if pesos == 1:       factor = 1.00
    elif pesos == 2:     factor = 1.00
    elif pesos == 3:     factor = 1.00
    elif pesos == 4:     factor = 1.00
    elif pesos == 5:     factor = 1.00
    elif pesos == 6:     factor = 1.00
    elif pesos == 7:     factor = 0.99
    elif pesos == 8:     factor = 0.99
    elif pesos == 9:     factor = 0.98
    elif pesos == 10:    factor = 0.98
    elif pesos == 20:    factor = 0.95
    else:                factor = 1.00

    # Total duration sa segundo
    total_seconds = pesos * base_seconds_per_peso * volume_ratio * factor
    return round(total_seconds, 2)


# ==============================================================================
# 💧 HELPER: Calculate Standard Recorded ML
# ==============================================================================
def calculate_standard_intake_ml(pesos, elapsed_seconds=None, total_target_seconds=None):
    """
    Tinitiyak na 100 mL per ₱1 PARIN ang lalabas sa App/Firebase Log.
    Kung may pause, kinukuha ang proportional value.
    """
    standard_total_ml = pesos * STANDARD_RECORD_RATE  # e.g., 1 peso = 100 mL

    if elapsed_seconds is not None and total_target_seconds is not None and total_target_seconds > 0:
        ratio = min(1.0, elapsed_seconds / total_target_seconds)
        return round(standard_total_ml * ratio, 1)
    
    return standard_total_ml


def initialize_firebase_system():
    try:
        if not firebase_admin._apps:
            if not os.path.exists(CRED_PATH):
                print(f"❌ FIREBASE ERROR: Hindi mahanap ang credential file sa: {CRED_PATH}")
                return False
                
            cred = credentials.Certificate(CRED_PATH)
            firebase_admin.initialize_app(cred, {
                'databaseURL': 'https://h2o-project-e83d9-default-rtdb.firebaseio.com'
            })
            print("✅ Firebase Admin SDK Successfully Initialized via Absolute Path!")
        
        print(f"🔗 Firebase targeting active node: vendos/{shared_state.VENDO_ID} ({shared_state.VENDO_NAME})")
        shared_state.vendo_ref = db.reference(f'vendos/{shared_state.VENDO_ID}')
        
        # Sync Initial Water Level
        firebase_water_percent = shared_state.vendo_ref.child('water_level').get()
        if firebase_water_percent is not None:
            shared_state.current_water_level = int((int(firebase_water_percent) / 100) * MAX_WATER_CAPACITY)
            print(f"📥 FIREBASE SYNC: Kasalukuyang laman sa cloud ay {firebase_water_percent}% ({shared_state.current_water_level}mL)")
        else:
            shared_state.current_water_level = MAX_WATER_CAPACITY
            shared_state.vendo_ref.child('water_level').set(100)
            print(f"📥 FIREBASE INITIALIZED: Itinakda sa 100% ({MAX_WATER_CAPACITY}mL)")

        # 💧 WATER REFILL LISTENER
        def water_level_listener(event):
            if event.data is not None:
                try:
                    val = int(event.data)
                    if val == 100 and shared_state.current_water_level < MAX_WATER_CAPACITY:
                        shared_state.current_water_level = MAX_WATER_CAPACITY
                        print(f"\n🔄 [LIVE EVENT] ADMIN REFILL DETECTED: Internal water level successfully reset to {MAX_WATER_CAPACITY}mL!")
                except Exception:
                    pass
                    
        shared_state.vendo_ref.child('water_level').listen(water_level_listener)

        # 🚀 BACKGROUND THREADS
        threading.Thread(target=start_heartbeat_loop, daemon=True).start()
        threading.Thread(target=listen_for_price_config, daemon=True).start()
        threading.Thread(target=listen_for_admin_commands, daemon=True).start()
        return True
    except Exception as e:
        print(f"❌ Firebase Connection Error: {e}")
        return False

def update_vending_status(status, water_level):
    if shared_state.vendo_ref:
        try:
            shared_state.vendo_ref.update({
                'name': shared_state.VENDO_NAME,
                'wifi_status': status,
                'water_level': water_level,
                'last_online': time.strftime("%Y-%m-%d %H:%M:%S")
            })
        except Exception as e:
            print(f"⚠️ Failed to update firebase status: {e}")

def start_heartbeat_loop():
    while True:
        try:
            water_percentage = round((shared_state.current_water_level / MAX_WATER_CAPACITY) * 100)
            update_vending_status("Connected", water_percentage)
        except Exception:
            pass
        time.sleep(10) 

# 💧 DYNAMIC VOLUME PER PESO LISTENER (CALIBRATION ONLY)
def listen_for_price_config():
    print("⚙️ Volume per Peso Listener Active...")
    
    def volume_adjust_listener(event):
        if event.data is not None:
            try:
                dynamic_ml_per_peso = float(event.data)
                
                if dynamic_ml_per_peso <= 0:
                    return

                # I-save para sa Hardware/Pump Calibration lamang!
                shared_state.LIVE_ML_PER_PESO = dynamic_ml_per_peso
                
                # ❌ Wag papalitan ang target_dispense_ml ng 150ml para sa User Log/App!
                # Keep target_dispense_ml fixed sa standard 100 mL per peso:
                shared_state.target_dispense_ml = STANDARD_RECORD_RATE
                
                print(f"⚙ [ADMIN CALIBRATION ADJUSTED]: Calibration output set to {dynamic_ml_per_peso} mL/₱1 (Standard App Log remains 100mL/₱1)")

                if hasattr(shared_state, 'app_instance') and shared_state.app_instance:
                    if hasattr(shared_state.app_instance, 'update_volume_display'):
                        shared_state.app_instance.update_volume_display(STANDARD_RECORD_RATE)
            except Exception as e:
                print(f"⚠️ Error parsing volume config: {e}")

    shared_state.vendo_ref.child('settings/ml_per_peso').listen(volume_adjust_listener)


# 📡 SERIAL LISTENER LOGIC PARA SA PAUSE & PROGRESS UPDATES TO FIREBASE
def handle_esp32_serial_message(line):
    """
    Prosesuhin ang mga Serial outputs mula kay ESP32 kapag nag-Pause o natapos.
    """
    line = line.strip()

    # ⏸️ PAUSE DETECTED
    if line.startswith("PAUSED_AT_SECONDS:"):
        try:
            paused_sec = float(line.split(":")[1])
            total_sec = getattr(shared_state, 'current_target_seconds', 4.10)
            pesos = getattr(shared_state, 'current_session_pesos', 1)

            # Kuwentahin ang na-dispense na tubig base sa Standard 100mL/₱1
            partial_standard_ml = calculate_standard_intake_ml(pesos, elapsed_seconds=paused_sec, total_target_seconds=total_sec)

            print(f"⏸️ [PAUSE DETECTED]: Na-pause sa {paused_sec}s / {total_sec}s. Water logged: {partial_standard_ml} mL")

            # Update sa Firebase / Mobile App para sa active student/session
            if shared_state.vendo_ref and getattr(shared_state, 'active_student_uid', None):
                shared_state.vendo_ref.child(f"active_session/{shared_state.active_student_uid}").update({
                    'dispensed_ml': partial_standard_ml,
                    'status': 'PAUSED'
                })
        except Exception as e:
            print(f"⚠️ Error handling PAUSED_AT_SECONDS: {e}")

    # 🏁 FINAL DISPENSED TIME
    elif line.startswith("DISPENSED_TIME_SEC:"):
        try:
            final_sec = float(line.split(":")[1])
            total_sec = getattr(shared_state, 'current_target_seconds', 4.10)
            pesos = getattr(shared_state, 'current_session_pesos', 1)

            final_standard_ml = calculate_standard_intake_ml(pesos, elapsed_seconds=final_sec, total_target_seconds=total_sec)

            print(f"✅ [DISPENSE FINISHED]: Total Time: {final_sec}s. Final Water Logged: {final_standard_ml} mL")

            # Bawasan ang water tank level sa Vendo
            shared_state.current_water_level = max(0, shared_state.current_water_level - int(final_standard_ml))
            water_percentage = round((shared_state.current_water_level / MAX_WATER_CAPACITY) * 100)
            update_vending_status("Connected", water_percentage)

            # Update sa Active Session para sa Mobile App
            if shared_state.vendo_ref and getattr(shared_state, 'active_student_uid', None):
                shared_state.vendo_ref.child(f"active_session/{shared_state.active_student_uid}").update({
                    'dispensed_ml': final_standard_ml,
                    'status': 'COMPLETED'
                })
        except Exception as e:
            print(f"⚠️ Error handling DISPENSED_TIME_SEC: {e}")


# 📡 ADMIN FORCE DISPENSE COMMAND LISTENER
def listen_for_admin_commands():
    print("📡 Admin Command Listener Active (Watching for Force Dispense)...")
    
    def listener(event):
        if event.data is True:
            if shared_state.active_student_uid is None:
                print("\n🚨 [WEB APP COMMAND] Force Dispense triggered in Standby Mode!")
                
                if hasattr(shared_state, 'esp32') and shared_state.esp32:
                    pesos = 1
                    target_seconds = calculate_dispense_seconds(pesos=pesos)
                    
                    shared_state.current_target_seconds = target_seconds
                    shared_state.current_session_pesos = pesos

                    command_to_send = f"START_PUMP_SEC:{target_seconds:.2f}\n"

                    with shared_state.serial_lock:
                        shared_state.esp32.reset_input_buffer()
                        shared_state.esp32.write(command_to_send.encode())

                    print(f"📡 [SERIAL SENT - STANDBY BYPASS]: {command_to_send.strip()} ({target_seconds}s)")
                    
                    if hasattr(shared_state, 'app_instance') and shared_state.app_instance:
                        shared_state.app_instance.update_status_label("🚨 Admin Force Dispense Active!", "#e67e22")
                    
                    # Standard 100 mL log para sa Tank Level reduction
                    standard_record_ml = STANDARD_RECORD_RATE * pesos
                    shared_state.current_water_level = max(0, shared_state.current_water_level - int(standard_record_ml))
                    water_percentage = round((shared_state.current_water_level / MAX_WATER_CAPACITY) * 100)
                    update_vending_status("Connected", water_percentage)
                else:
                    print("❌ Cannot dispense: ESP32 connection is offline!")
                
                try:
                    shared_state.vendo_ref.update({'force_dispense': False})
                    print("✅ Standby override done! Firebase flag reset to False.")
                    if hasattr(shared_state, 'app_instance') and shared_state.app_instance:
                        shared_state.app_instance.update_status_label("⏳ Ready to Scan QR Code", "#2ecc71")
                except Exception as fb_err:
                    print(f"⚠️ Error resetting force_dispense flag: {fb_err}")
            else:
                print("\n🚨 [WEB APP COMMAND] Force Dispense detected! Transaction active, passing control to hardware loop...")

    shared_state.vendo_ref.child('force_dispense').listen(listener)