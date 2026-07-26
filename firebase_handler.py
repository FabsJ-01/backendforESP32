import firebase_admin
from firebase_admin import credentials, db
import time
import threading
import sys
import shared_state

def initialize_firebase_system():
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate("key.json")
            firebase_admin.initialize_app(cred, {
                'databaseURL': 'https://h2o-project-e83d9-default-rtdb.firebaseio.com'
            })
        
        print(f"🔗 Firebase targeting active node: vendos/{shared_state.VENDO_ID} ({shared_state.VENDO_NAME})")
        shared_state.vendo_ref = db.reference(f'vendos/{shared_state.VENDO_ID}')
        
        firebase_water_percent = shared_state.vendo_ref.child('water_level').get()
        if firebase_water_percent is not None:
            shared_state.current_water_level = int((int(firebase_water_percent) / 100) * 16000)
            print(f"📥 FIREBASE SYNC: Kasalukuyang laman sa cloud ay {firebase_water_percent}% ({shared_state.current_water_level}mL)")
        else:
            shared_state.current_water_level = 16000
            shared_state.vendo_ref.child('water_level').set(100)
            print(f"📥 FIREBASE INITIALIZED: Itinakda sa 100%")

        def water_level_listener(event):
            if event.data is not None:
                try:
                    val = int(event.data)
                    if val == 100 and shared_state.current_water_level < 16000:
                        shared_state.current_water_level = 16000
                        print("\n🔄 [LIVE EVENT] ADMIN REFILL DETECTED: Internal water level successfully reset to 16000mL!")
                except Exception:
                    pass
                    
        shared_state.vendo_ref.child('water_level').listen(water_level_listener)

        # 🚀 MGA BACKGROUND THREADS
        # NOTE: listen_for_admin_commands ay dito LANG dapat pinapaandar - HUWAG na
        # itong i-start ulit sa hardware.py, dahil doble ang magiging listener kung
        # gagawin (dalawang beses tatakbo ang bawat force_dispense trigger, sanhi ng
        # sabay-sabay na pagsulat sa serial port).
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
            water_percentage = round((shared_state.current_water_level / 16000) * 100)
            update_vending_status("Connected", water_percentage)
        except Exception:
            pass
        time.sleep(5) 

def listen_for_price_config():
    def price_listener(event):
        if event.data is not None:
            try:
                shared_state.LIVE_ML_PER_PESO = int(event.data)
                print(f"\n⚙️ CLOUD CONFIG UPDATE: new ratio for Admin: ₱1 = {shared_state.LIVE_ML_PER_PESO}mL")
                # NOTE: Wala nang SET_RATIO command sa bagong time-based firmware -
                # si Python na mismo ang direktang nagko-compute ng milliseconds gamit
                # ang LIVE_ML_PER_PESO sa oras ng dispensing (tignan ang hardware.py),
                # kaya wala nang kailangang i-sync na command papunta sa ESP32 dito.
                # Tinanggal din ang dating direktang esp32.write() dito dahil
                # nagdudulot ito ng race condition/garbled data (walang lock dati).
            except Exception as e:
                print(f"⚠️ Error parsing price config: {e}")

    shared_state.vendo_ref.child('settings/ml_per_peso').listen(price_listener)

def listen_for_admin_commands():
    print("📡 Admin Command Listener Active (Watching for Force Dispense)...")
    
    def listener(event):
        if event.data is True:
            if shared_state.active_student_uid is None:
                print("\n🚨 [WEB APP COMMAND] Force Dispense triggered in Standby Mode!")
                
                if shared_state.esp32:
                    admin_pesos = 2.5
                    admin_test_ms = int(admin_pesos * 2500.0)
                    command_to_send = f"START_PUMP_MS:{admin_test_ms}\n"

                    # === AYOS: Ginamit na ang serial_lock para hindi mag-collide sa
                    # ibang thread (hardware_listener_loop, process_scanned_student)
                    # na parehong gumagalaw sa serial port sa parehong sandali. ===
                    with shared_state.serial_lock:
                        shared_state.esp32.reset_input_buffer()
                        shared_state.esp32.write(command_to_send.encode())

                    print(f"📡 [SERIAL SENT - STANDBY BYPASS]: {command_to_send.strip()}")
                    
                    if hasattr(shared_state, 'app_instance') and shared_state.app_instance:
                        shared_state.app_instance.update_status_label("🚨 Admin Force Dispense Active!", "#e67e22")
                    
                    simulated_ml = admin_pesos * shared_state.LIVE_ML_PER_PESO
                    shared_state.current_water_level = max(0, shared_state.current_water_level - simulated_ml)
                    
                    shared_state.is_flow_monitoring_mode = True
                    time.sleep(admin_test_ms / 1000.0)
                    shared_state.is_flow_monitoring_mode = False
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