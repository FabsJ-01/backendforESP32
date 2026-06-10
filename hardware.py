import serial
import time
import threading
from firebase_admin import db
import shared_state
import firebase_handler

def process_scanned_student(scanned_uid):
    scanned_uid = scanned_uid.strip()
    if not scanned_uid: 
        return

    user_ref = db.reference(f'users/{scanned_uid}')
    user_data = user_ref.get()
    if not user_data:
        print(f"❌ User {scanned_uid} not found.")
        if shared_state.app_instance:
            shared_state.app_instance.update_status_label(f"❌ UID {scanned_uid} Not Found!", "#e74c3c")
        shared_state.active_student_uid = None 
        return

    shared_state.active_student_uid = scanned_uid
    user_ref.update({'coin_trigger': False, 'is_scanning': False, 'last_credits': 0})
    welcome_msg = f"👋 Welcome, {user_data.get('name', 'Student')}!"
    print(welcome_msg)
    
    if shared_state.app_instance:
        shared_state.app_instance.update_status_label(welcome_msg, "#3498db")

    if shared_state.esp32: 
        shared_state.esp32.reset_input_buffer()
        shared_state.esp32.write(b'READY_FOR_COINS\n')

    shared_state.coin_amount = 0
    shared_state.last_coin_time = time.time()
    shared_state.is_coin_accumulation_mode = True

    print(f"🪙 Waiting for coins for user: {scanned_uid} (Current Ratio: ₱1 = {shared_state.LIVE_ML_PER_PESO}mL)...")
    
    while shared_state.is_coin_accumulation_mode:
        try:
            if shared_state.vendo_ref.child('force_dispense').get() is True:
                shared_state.coin_amount = 0
                shared_state.is_coin_accumulation_mode = False
                break
        except Exception: 
            pass
            
        if shared_state.coin_amount > 0 and (time.time() - shared_state.last_coin_time > shared_state.timeout_duration):
            print(f"\n⏳ Coin insertion timeout. Total accumulated: ₱{shared_state.coin_amount}")
            shared_state.is_coin_accumulation_mode = False
            break
            
        if shared_state.coin_amount == 0 and (time.time() - shared_state.last_coin_time > 40.0): 
            print(f"\n⏳ Kiosk Session Timeout. Walang baryang hinulog.")
            shared_state.is_coin_accumulation_mode = False
            break
            
        time.sleep(0.2) 

    if shared_state.coin_amount == 0: 
        shared_state.active_student_uid = None 
        if shared_state.app_instance: shared_state.app_instance.update_status_label("⏳ Ready to Scan QR Code", "#2ecc71")
        return

    shared_state.ml_to_dispense = shared_state.coin_amount * shared_state.LIVE_ML_PER_PESO
    user_ref.update({'last_credits': shared_state.coin_amount, 'is_scanning': True})

    if shared_state.app_instance:
        shared_state.app_instance.update_status_label(f"🪙 Final Total: ₱{shared_state.coin_amount} ({shared_state.ml_to_dispense}mL). Tap Dispense on Phone!", "#f1c40f")

    print("📱 Waiting for Flutter App 'Dispense' click...")
    while True:
        try:
            if shared_state.vendo_ref.child('force_dispense').get() is True:
                break
            current_status = user_ref.get()
            if current_status and current_status.get('coin_trigger') == True:
                
                if shared_state.esp32: 
                    shared_state.esp32.reset_input_buffer()
                    shared_state.is_flow_monitoring_mode = True 
                    shared_state.esp32.write(f"START_PUMP_ML:{shared_state.ml_to_dispense}\n".encode())
                
                while shared_state.is_flow_monitoring_mode:
                    time.sleep(0.05) 
                
                shared_state.current_water_level = max(0, shared_state.current_water_level - shared_state.ml_to_dispense)
                water_percentage = round((shared_state.current_water_level / 16000) * 100)
                
                try:
                    shared_state.vendo_ref.update({'water_level': water_percentage})
                    print(f"\n📉 GALLON UPDATE: {shared_state.current_water_level}mL remaining ({water_percentage}%)")
                except Exception as e:
                    print(f"⚠️ Immediate water level push error: {e}")

                new_intake = (current_status.get('intake', 0) or 0) + shared_state.ml_to_dispense
                finish_time = time.strftime("%Y-%m-%d %H:%M:%S")
                
                user_ref.update({
                    'intake': new_intake, 'last_drink_time': finish_time,
                    'is_scanning': False, 'coin_trigger': False, 'last_credits': 0
                })
            
                user_psu_id = user_data.get('psu_id', 'N/A') 

                db.reference('dispense_logs').push({
                    'uid': scanned_uid, 
                    'psu_id': user_psu_id,  
                    'name': user_data.get('name', 'Unknown'),
                    'course': user_data.get('course', 'Unknown'), 
                    'section': user_data.get('section', 'Unknown'), 
                    'vendo_id': shared_state.VENDO_ID, 
                    'amount_ml': shared_state.ml_to_dispense, 
                    'timestamp': finish_time, 
                    'status': "Success"
                })
                break
        except Exception as e:
            print(f"⚠️ Error inside active listen loop: {e}")
            
        time.sleep(0.4) 
        
    shared_state.active_student_uid = None
    print("🔒 Kiosk lock released. Ready for the next transaction.")
    if shared_state.app_instance: shared_state.app_instance.update_status_label("⏳ Ready to Scan QR Code", "#2ecc71")


def start_h2o_core_system():
    print("\n--- H2O HUB: SMART SYSTEM RUNNING ---")
    try:
        shared_state.esp32 = serial.Serial(shared_state.SERIAL_PORT, shared_state.BAUD_RATE, timeout=0.1)
        print(f"✅ Hardware Linked: {shared_state.SERIAL_PORT}")
        time.sleep(2) 
        if shared_state.esp32.is_open:
            shared_state.esp32.write(f"SET_RATIO:{shared_state.LIVE_ML_PER_PESO}\n".encode())
            print(f"⚙️ Initial Hardware Sync Transmitted -> SET_RATIO:{shared_state.LIVE_ML_PER_PESO}")
            
    except Exception as e:
        print(f"⚠️ Hardware Offline: {e}")
        shared_state.esp32 = None

    if not firebase_handler.initialize_firebase_system():
        return

    threading.Thread(target=firebase_handler.listen_for_admin_commands, daemon=True).start()

    def hardware_listener_loop():
        while True:
            if shared_state.esp32 and shared_state.esp32.in_waiting > 0:
                try:
                    hardware_data = shared_state.esp32.readline().decode('utf-8', errors='ignore').strip()
                    if not hardware_data:
                        continue
                    
                    print(f"📡 [RAW HARDWARE DATA]: {hardware_data}") 
                    
                    if hardware_data.startswith("UID_"): 
                        uid = hardware_data.replace("UID_", "").strip()
                        if shared_state.active_student_uid is not None:
                            print(f"⚠️ KIOSK LOCK ACTIVE: Tinanggihan si {uid} (Kasalukuyang may gumagamit).")
                            if shared_state.app_instance:
                                shared_state.app_instance.update_status_label("⚠️ System Busy! Finish current transaction first.", "#e74c3c")
                            continue
                        threading.Thread(target=process_scanned_student, args=(uid,), daemon=True).start()
                    
                    elif shared_state.is_coin_accumulation_mode and "DETECTED" in hardware_data:
                        shared_state.last_coin_time = time.time()  
                        coin_detected = 0
                        if "1_PESO" in hardware_data: coin_detected = 1
                        elif "5_PESOS" in hardware_data: coin_detected = 5
                        elif "10_PESOS" in hardware_data: coin_detected = 10
                        elif "20_PESOS" in hardware_data: coin_detected = 20
                        
                        if coin_detected > 0:
                            shared_state.coin_amount += coin_detected
                            print(f"\n🪙 Central Engine Log - Coin Added: ₱{coin_detected} | Total Accumulated: ₱{shared_state.coin_amount}")
                            
                            if shared_state.app_instance: 
                                shared_state.app_instance.update_status_label(f"🪙 Total Coins: ₱{shared_state.coin_amount} ({shared_state.coin_amount * shared_state.LIVE_ML_PER_PESO}mL).", "#f1c40f")
                            
                            try:
                                db.reference(f'users/{shared_state.active_student_uid}').update({'last_credits': shared_state.coin_amount, 'is_scanning': True})
                            except Exception as cloud_err:
                                print(f"⚠️ Cloud sync error during insertion: {cloud_err}")

                    elif shared_state.is_flow_monitoring_mode:
                        if hardware_data.startswith("LIVE_DISPENSE_ML:"):
                            try:
                                current_ml = hardware_data.split(":")[1].strip()
                                status_txt = f"💧 Flow Meter: {current_ml}mL / {shared_state.ml_to_dispense}mL poured"
                                print(f"\r{status_txt}", end="")
                                if shared_state.app_instance: shared_state.app_instance.update_status_label(status_txt, "#e67e22")
                            except (IndexError, ValueError):
                                pass 
                            
                        elif "TARGET_REACHED" in hardware_data or "PUMP_OFF" in hardware_data:
                            print("\n✅ Target reached perfectly! Flow sensor triggered hardware cut-off.")
                            shared_state.is_flow_monitoring_mode = False 

                except Exception as e:
                    print(f"⚠️ Serial parsing single line error (Thread Protected): {e}")
            time.sleep(0.02) 

    threading.Thread(target=hardware_listener_loop, daemon=True).start()