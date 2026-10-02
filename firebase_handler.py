import os
import firebase_admin
from firebase_admin import credentials, db
import time
import threading
import sys
import shared_state

MAX_WATER_CAPACITY = 20000  # 20 Liters = 20,000 mL

# 🎯 DYNAMIC ABSOLUTE PATH SETUP PARA SA KEY.JSON
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CRED_PATH = os.path.join(BASE_DIR, "key.json")

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
        threading.Thread(target=listen_for_flow_calibration, daemon=True).start()
        threading.Thread(target=listen_for_admin_commands, daemon=True).start()
        threading.Thread(target=listen_for_test_dispense, daemon=True).start()
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

def listen_for_price_config():
    def price_listener(event):
        if event.data is not None:
            try:
                shared_state.LIVE_ML_PER_PESO = int(event.data)
                print(f"\n⚙️ CLOUD CONFIG UPDATE: new ratio for Admin: ₱1 = {shared_state.LIVE_ML_PER_PESO}mL")
            except Exception as e:
                print(f"⚠️ Error parsing price config: {e}")

    shared_state.vendo_ref.child('settings/ml_per_peso').listen(price_listener)

def listen_for_flow_calibration():
    print("🌊 Flow Sensor Calibration Listener Active...")
    def calibration_listener(event):
        if event.data is not None:
            try:
                pulses_val = float(event.data)
                print(f"\n⚙️ [FLOW CALIBRATION] New pulse calibration received: {pulses_val} pulses/mL")
                
                if shared_state.esp32:
                    cmd = f"SET_PULSES_PER_ML:{pulses_val:.2f}\n"
                    with shared_state.serial_lock:
                        shared_state.esp32.write(cmd.encode())
                    print(f"📡 [SERIAL SENT - CALIBRATION]: {cmd.strip()}")
                else:
                    print("⚠️ ESP32 offline, calibration saved in cloud but not synced to hardware yet.")
            except Exception as e:
                print(f"⚠️ Error parsing flow calibration: {e}")

    shared_state.vendo_ref.child('settings/pulses_per_ml').listen(calibration_listener)

def listen_for_admin_commands():
    print("📡 Admin Command Listener Active (Watching for Force Dispense)...")
    
    def listener(event):
        if event.data is True:
            if shared_state.active_student_uid is None:
                print("\n🚨 [WEB APP COMMAND] Force Dispense triggered in Standby Mode!")
                
                if shared_state.esp32:
                    admin_pesos = 2.5
                    target_ml = float(admin_pesos * shared_state.LIVE_ML_PER_PESO)
                    command_to_send = f"START_PUMP_ML:{target_ml:.1f}\n"

                    with shared_state.serial_lock:
                        shared_state.esp32.reset_input_buffer()
                        shared_state.esp32.write(command_to_send.encode())

                    print(f"📡 [SERIAL SENT - STANDBY BYPASS]: {command_to_send.strip()}")
                    
                    if hasattr(shared_state, 'app_instance') and shared_state.app_instance:
                        shared_state.app_instance.update_status_label("🚨 Admin Force Dispense Active!", "#e67e22")
                    
                    shared_state.current_water_level = max(0, shared_state.current_water_level - int(target_ml))
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
                print("\n🚨 [WEB APP COMMAND] Force Dispense detected! Transaction active, passing control to hardware.py loop...")

    shared_state.vendo_ref.child('force_dispense').listen(listener)

# 🧪 TEST DISPENSE LISTENER: Sinusuportahan ang 'test_dispense' at 'test_dispense_ms'
def listen_for_test_dispense():
    print("🧪 Test Dispense Listener Active (Watching for 5-Second Test)...")
    
    def test_listener(event):
        # Huwag pansinin kapag nireset pabalik sa False / 0 / None
        if event.data is None or event.data is False or event.data == 0:
            return

        # Tumatanggap ng True, "TEST_5s", o anumang millisecond value (e.g. 5000)
        if event.data is True or event.data == "TEST_5s" or str(event.data) == "5000":
            print("\n🧪 [WEB APP COMMAND] 5-Second Test Dispense Triggered!")
            
            if hasattr(shared_state, 'esp32') and shared_state.esp32:
                command_to_send = "START_TEST_5S\n"

                with shared_state.serial_lock:
                    shared_state.esp32.reset_input_buffer()
                    shared_state.esp32.write(command_to_send.encode())

                print(f"📡 [SERIAL SENT - TEST DISPENSE]: {command_to_send.strip()}")
            else:
                print("❌ Cannot run test: ESP32 connection is offline!")
            
            # 🚀 IMPORTANT: I-reset ang PAREHONG flags sa Firebase para pwedeng pindutin ulit!
            try:
                shared_state.vendo_ref.update({
                    'test_dispense': False,
                    'test_dispense_ms': 0
                })
                print("✅ Test dispense flags successfully reset to False/0 in Firebase.")
            except Exception as fb_err:
                print(f"⚠️ Error resetting test dispense flags: {fb_err}")

    # Pakinggan ang 'test_dispense' node
    shared_state.vendo_ref.child('test_dispense').listen(test_listener)
    
    # Backup listener para sa 'test_dispense_ms' node (kung ito ang gamit sa Flutter)
    shared_state.vendo_ref.child('test_dispense_ms').listen(test_listener)