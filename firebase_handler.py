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

        threading.Thread(target=start_heartbeat_loop, daemon=True).start()
        threading.Thread(target=listen_for_price_config, daemon=True).start()
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
            water_percentage = round((shared_state.current_water_level / 20000) * 100)
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
                
                if shared_state.esp32 and shared_state.esp32.is_open:
                    time.sleep(0.1)
                    shared_state.esp32.write(f"SET_RATIO:{shared_state.LIVE_ML_PER_PESO}\n".encode())
                    print(f"📡 Serial transmitted to ESP32 -> SET_RATIO:{shared_state.LIVE_ML_PER_PESO}")
                    
            except Exception as e:
                print(f"⚠️ Error parsing or sending price config: {e}")
                
    shared_state.vendo_ref.child('settings/ml_per_peso').listen(price_listener)

def listen_for_admin_commands():
    def listener(event):
        if event.data is True:
            print("\n🚀 ADMIN COMMAND: Force Dispensing Water...")
            if shared_state.esp32: shared_state.esp32.write(b'START_PUMP_ML:250\n')
            
            for remaining in range(5, 0, -1):
                sys.stdout.write(f"\r⏳ Admin dispensing override active... {remaining}s")
                sys.stdout.flush()
                time.sleep(1)
            print("\r✅ Override done!          ")
            if shared_state.esp32: shared_state.esp32.write(b'STOP_PUMP\n')
            
            if shared_state.active_student_uid:
                db.reference(f'users/{shared_state.active_student_uid}').update({
                    'coin_trigger': False, 'is_scanning': False, 'last_credits': 0
                })
            shared_state.vendo_ref.update({'force_dispense': False}) 

    shared_state.vendo_ref.child('force_dispense').listen(listener)