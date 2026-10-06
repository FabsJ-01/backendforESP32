import serial
import serial.tools.list_ports
import time
import threading
from firebase_admin import db
import shared_state
import firebase_handler

# ============================================
# SETTINGS
# ============================================
FIREBASE_RETRY_DELAY = 5        # segundo sa pagitan ng bawat retry ng Firebase
SAFETY_TIMEOUT_SECONDS = 90.0   # max na oras ng PUMPING (hindi kasama ang oras na naka-pause)
DEFAULT_WATER_CAPACITY = 20000  # gagamitin kung walang MAX_WATER_CAPACITY sa firebase_handler


def _water_capacity():
    return getattr(firebase_handler, 'MAX_WATER_CAPACITY', DEFAULT_WATER_CAPACITY)


def _set_status(text, color):
    """Ligtas na pag-update ng GUI label (hindi mag-crash kung wala pang GUI)."""
    try:
        if shared_state.app_instance:
            shared_state.app_instance.update_status_label(text, color)
    except Exception:
        pass


# ============================================
# PROCESS SCANNED STUDENT (QR / RFID LOGIC)
# ============================================
def process_scanned_student(scanned_uid):
    scanned_uid = scanned_uid.strip()
    if not scanned_uid:
        return

    user_ref = db.reference(f'users/{scanned_uid}')
    user_data = user_ref.get()
    if not user_data:
        print(f"❌ User {scanned_uid} not found.")
        _set_status(f"❌ UID {scanned_uid} Not Found!", "#e74c3c")
        shared_state.active_student_uid = None
        return

    shared_state.active_student_uid = scanned_uid
    user_ref.update({'coin_trigger': False, 'is_scanning': False, 'last_credits': 0})
    welcome_msg = f"👋 Welcome, {user_data.get('name', 'Student')}!"
    print(welcome_msg)
    _set_status(welcome_msg, "#3498db")

    shared_state.coin_amount = 0
    shared_state.last_coin_time = time.time()
    shared_state.is_coin_accumulation_mode = True

    if shared_state.esp32:
        try:
            with shared_state.serial_lock:
                shared_state.esp32.reset_input_buffer()
                shared_state.esp32.write(b'READY_FOR_COINS\n')
        except Exception as e:
            print(f"⚠️ Serial Write Error (READY_FOR_COINS): {e}")

    print(f"🪙 Waiting for coins for user: {scanned_uid}...")

    # 1. COIN ACCUMULATION LOOP
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
            if shared_state.esp32:
                try:
                    with shared_state.serial_lock:
                        shared_state.esp32.write(b'TIMEOUT_RESET\n')
                except Exception as e:
                    print(f"⚠️ Serial Write Error (TIMEOUT_RESET): {e}")
            shared_state.is_coin_accumulation_mode = False
            break

        time.sleep(0.2)

    if shared_state.coin_amount == 0:
        shared_state.active_student_uid = None
        _set_status("⏳ Ready to Scan QR Code", "#2ecc71")
        return

    shared_state.ml_to_dispense = shared_state.coin_amount * shared_state.LIVE_ML_PER_PESO
    user_ref.update({'last_credits': shared_state.coin_amount, 'is_scanning': True})

    _set_status(
        f"🪙 Final Total: ₱{shared_state.coin_amount} ({shared_state.ml_to_dispense:.0f}mL). Tap Dispense on Phone!",
        "#f1c40f"
    )

    print("📱 Waiting for Mobile App 'Dispense' click...")

    # 2. DISPENSE TRIGGER WAIT LOOP
    dispense_started = False

    # === IDLE TIMEOUT CONFIGURATION ===
    IDLE_TIMEOUT_SECONDS = 30.0  # Bibigyan ng 30 seconds ang user bago mag-timeout kung walang galaw
    wait_start_time = time.time()

    # 🔴 DAPAT NAKA-INITIALIZE DITO BAGO MAG-WHILE LOOP
    previous_credits = 0
    while True:
        try:
            if shared_state.vendo_ref.child('force_dispense').get() is True:
                break

            current_status = user_ref.get()

            # ----------------------------------------------------
            # A. KAPAG NAG-TRIGGER NA NG DISPENSE (COIN / BUTTON)
            # ----------------------------------------------------
            if current_status and current_status.get('coin_trigger') == True:

                dispense_started = True
                shared_state.actual_dispensed_ml = 0.0

                if shared_state.esp32:
                    shared_state.is_flow_monitoring_mode = True
                    shared_state.is_pump_paused = False
                    shared_state.paused_time_offset = 0.0

                    target_ml = float(shared_state.ml_to_dispense)
                    command_to_send = f"START_PUMP_ML:{target_ml:.1f}\n"

                    with shared_state.serial_lock:
                        shared_state.esp32.reset_input_buffer()
                        shared_state.esp32.write(command_to_send.encode())

                    print(f"📡 [SERIAL SENT]: {command_to_send.strip()} para sa {shared_state.ml_to_dispense}mL")

                start_flow_time = time.time()

                # 3. DISPENSING PROGRESS MONITORING LOOP
                while shared_state.is_flow_monitoring_mode:
                    # Ang oras na naka-pause ay HINDI binibilang sa safety timeout
                    if not getattr(shared_state, 'is_pump_paused', False):
                        active_time = (time.time() - start_flow_time) - getattr(shared_state, 'paused_time_offset', 0.0)
                        if active_time > SAFETY_TIMEOUT_SECONDS:
                            print(f"\n⚠️ [SAFETY TIMEOUT] {SAFETY_TIMEOUT_SECONDS:.0f}s max pumping time. Forcing stop.")
                            if shared_state.esp32:
                                try:
                                    with shared_state.serial_lock:
                                        shared_state.esp32.write(b'STOP_PUMP\n')
                                except Exception as e:
                                    print(f"⚠️ Serial Write Error (STOP_PUMP): {e}")
                            
                            # Hintayin sandali ang huling final response ng ESP32
                            wait_start = time.time()
                            while shared_state.is_flow_monitoring_mode and (time.time() - wait_start) < 3.0:
                                time.sleep(0.05)
                            shared_state.is_flow_monitoring_mode = False
                            break

                    time.sleep(0.05)

                # Bigyan ng konting pahinga (0.5s) upang makahabol ang anumang natitirang Serial Message mula sa ESP32
                time.sleep(0.5)

                # ========================================================
                # VALIDATION AT UPDATE NG NAIBUHOS NA TUBIG (PAUSE / TIMEOUT SAFE)
                # ========================================================
                poured_ml = float(getattr(shared_state, 'actual_dispensed_ml', 0.0))
                poured_ml = min(poured_ml, float(shared_state.ml_to_dispense))
                poured_ml = round(poured_ml, 1)

                print(f"\n📊 FINAL DISPENSED AMOUNT TO RECORD: {poured_ml} mL (Target was {shared_state.ml_to_dispense} mL)")

                if poured_ml <= 0:
                    print("⚠️ 0 mL dispensed. Skipping Firebase intake update and logs.")
                    user_ref.update({
                        'is_scanning': False,
                        'coin_trigger': False,
                        'last_credits': 0
                    })
                    break

                # Update gallon water level
                shared_state.current_water_level = max(0, shared_state.current_water_level - int(poured_ml))
                water_percentage = round((shared_state.current_water_level / _water_capacity()) * 100)

                try:
                    shared_state.vendo_ref.update({'water_level': water_percentage})
                    print(f"📉 GALLON UPDATE: {shared_state.current_water_level}mL remaining ({water_percentage}%)")
                except Exception as e:
                    print(f"⚠️ Water level push error: {e}")

                # KUNIN AT IPAGPATONG (ACCUMULATE) SA KASALUKUYANG INTAKE NG USER
                current_user_data = user_ref.get() or {}
                raw_intake = current_user_data.get('intake', 0) or 0
                
                # Siguraduhing float value ang nareread
                if isinstance(raw_intake, str):
                    raw_intake = raw_intake.replace('M', '').replace('m', '').strip()
                
                old_intake = float(raw_intake)
                new_intake = round(old_intake + poured_ml, 1)
                finish_time = time.strftime("%Y-%m-%d %H:%M:%S")

                user_ref.update({
                    'intake': new_intake,
                    'last_drink_time': finish_time,
                    'is_scanning': False,
                    'coin_trigger': False,
                    'last_credits': 0
                })

                print(f"🥛 [SUCCESS INTAKE UPDATE]: Old Intake: {old_intake} mL + Added: {poured_ml} mL = New Intake: {new_intake} mL")

                user_psu_id = user_data.get('psu_id', 'N/A')
                is_full = poured_ml >= (shared_state.ml_to_dispense - 5)

                db.reference('dispense_logs').push({
                    'uid': scanned_uid,
                    'psu_id': user_psu_id,
                    'name': user_data.get('name', 'Unknown'),
                    'course': user_data.get('course', 'Unknown'),
                    'section': user_data.get('section', 'Unknown'),
                    'vendo_id': shared_state.VENDO_ID,
                    'amount_ml': poured_ml,
                    'timestamp': finish_time,
                    'status': "Success" if is_full else "Partial (Paused/Timeout)"
                })
                break

            # ----------------------------------------------------
            # B. WALA PANG DISPENSE: CHECK IDLE TIMEOUT
            # ----------------------------------------------------
            else:
                current_credits = current_status.get('last_credits', 0) or 0
                
                # 🔹 I-reset LAMANG ang timer KUNG MAY BAGONG HULOG NA BARYA (tumaas ang credits)
                if current_credits > previous_credits:
                    print(f"🪙 [COIN DETECTED]: Credits updated to {current_credits}. Resetting idle timer...")
                    wait_start_time = time.time()
                    previous_credits = current_credits  # Update local reference

                # 🔹 Kapag WALANG BAGONG HULOG at lumagpas na sa IDLE_TIMEOUT_SECONDS:
                if (time.time() - wait_start_time) > IDLE_TIMEOUT_SECONDS:
                    print(f"\n⏰ [IDLE TIMEOUT] Lumagpas sa {IDLE_TIMEOUT_SECONDS:.0f}s na walang aksyon mula sa user. Clearing transaction...")
                    
                    user_ref.update({
                        'is_scanning': False,
                        'coin_trigger': False,
                        'last_credits': 0
                    })
                    
                    if shared_state.esp32:
                        try:
                            with shared_state.serial_lock:
                                shared_state.esp32.write(b'TIMEOUT_RESET\n')
                        except Exception as e:
                            print(f"⚠️ Serial Write Error (TIMEOUT_RESET): {e}")
                    
                    break

        except Exception as e:
            print(f"⚠️ Error inside active listen loop: {e}")
            if dispense_started:
                shared_state.is_flow_monitoring_mode = False
                try:
                    user_ref.update({'is_scanning': False, 'coin_trigger': False, 'last_credits': 0})
                except Exception:
                    pass
                break

        time.sleep(0.4)

    shared_state.active_student_uid = None
    print("🔒 Kiosk lock released. Ready for next transaction.")
    _set_status("⏳ Ready to Scan QR Code", "#2ecc71")

# ============================================
# HARDWARE CONNECTION & BACKGROUND LISTENER
# ============================================
def connect_to_esp32(max_retries=3, retry_delay=2):
    for attempt in range(1, max_retries + 1):
        current_port = shared_state.find_esp32_port()

        if current_port is None:
            print(f"❌ [Attempt {attempt}/{max_retries}] Walang nahanap na ESP32-S3 device.")
            time.sleep(retry_delay)
            continue

        shared_state.SERIAL_PORT = current_port

        try:
            shared_state.esp32 = serial.Serial(shared_state.SERIAL_PORT, shared_state.BAUD_RATE, timeout=0.1)
            print(f"✅ Hardware Linked: {shared_state.SERIAL_PORT}")
            time.sleep(2)
            return True

        except Exception as e:
            print(f"⚠️ [Attempt {attempt}/{max_retries}] Error: {e}")
            shared_state.esp32 = None
            time.sleep(retry_delay)

    shared_state.esp32 = None
    return False


def _esp32_watchdog():
    """Kapag na-unplug/nawala ang ESP32, susubukan nitong kumonekta ulit."""
    while True:
        if shared_state.esp32 is None:
            print("⚠️ ESP32 Disconnected. Attempting reconnection...")
            connect_to_esp32(max_retries=1, retry_delay=1)
        time.sleep(3.0)


def _start_hardware_listener():
    def hardware_listener_loop():
        consecutive_errors = 0

        while True:
            if shared_state.esp32:
                hardware_data = None

                try:
                    with shared_state.serial_lock:
                        if shared_state.esp32.in_waiting > 0:
                            hardware_data = shared_state.esp32.readline().decode('utf-8', errors='ignore').strip()
                    consecutive_errors = 0
                except Exception as read_err:
                    consecutive_errors += 1
                    print(f"⚠️ Serial Reading Error: {read_err}")
                    if consecutive_errors >= 5:
                        try:
                            shared_state.esp32.close()
                        except Exception:
                            pass
                        shared_state.esp32 = None
                        consecutive_errors = 0
                    time.sleep(0.2)
                    continue

                if not hardware_data:
                    time.sleep(0.02)
                    continue

                print(f"📡 [RAW HARDWARE DATA]: {hardware_data}")

                # 🚀 0. TEST DISPENSE PULSES RESULT HANDLER
                if hardware_data.startswith("TEST_PULSES_RESULT:"):
                    try:
                        total_pulses = int(hardware_data.split(":")[1].strip())
                        print(f"📥 [SERIAL RECEIVED] 5-Second Test Pulses: {total_pulses}")
                        if hasattr(shared_state, 'vendo_ref') and shared_state.vendo_ref:
                            shared_state.vendo_ref.update({
                                'last_test_pulses': total_pulses
                            })
                            print("✅ last_test_pulses successfully updated in Firebase!")
                    except Exception as e:
                        print(f"⚠️ Error parsing test pulses result: {e}")

                # 1. QR / CARD UID SCAN HANDLER
                elif hardware_data.startswith("UID_"):
                    uid = hardware_data.replace("UID_", "").strip()
                    if shared_state.active_student_uid is not None:
                        print(f"⚠️ KIOSK BUSY: Tinanggihan si {uid}.")
                        _set_status("⚠️ System Busy!", "#e74c3c")
                        continue
                    threading.Thread(target=process_scanned_student, args=(uid,), daemon=True).start()

                # 2. COIN INSERTION HANDLER
                elif shared_state.is_coin_accumulation_mode and ("_PESO" in hardware_data or "_PESOS" in hardware_data):
                    shared_state.last_coin_time = time.time()
                    coin_detected = 0

                    if "1_PESO" in hardware_data:
                        coin_detected = 1
                    elif "5_PESOS" in hardware_data:
                        coin_detected = 5
                    elif "10_PESOS" in hardware_data:
                        coin_detected = 10
                    elif "20_PESOS" in hardware_data:
                        coin_detected = 20

                    if coin_detected > 0:
                        shared_state.coin_amount += coin_detected
                        print(f"\n🪙 Coin Added: ₱{coin_detected} | Total Accumulated: ₱{shared_state.coin_amount}")
                        _set_status(
                            f"🪙 Total Coins: ₱{shared_state.coin_amount} ({shared_state.coin_amount * shared_state.LIVE_ML_PER_PESO:.0f}mL).",
                            "#f1c40f"
                        )

                # 3. PHYSICAL BUTTON PAUSE/RESUME HANDLER
                elif hardware_data == "PUMP_PAUSED":
                    shared_state.is_pump_paused = True
                    shared_state.pause_started_at = time.time()
                    print("\n⏸️ Pump paused via physical button.")
                    _set_status("⏸️ Dispensing Paused (Pindutin ang button sa Kiosk para ituloy)", "#F59E0B")

                elif hardware_data == "PUMP_RESUMED":
                    if getattr(shared_state, 'is_pump_paused', False):
                        pause_duration = time.time() - getattr(shared_state, 'pause_started_at', time.time())
                        shared_state.paused_time_offset = getattr(shared_state, 'paused_time_offset', 0.0) + pause_duration
                    
                    shared_state.is_pump_paused = False
                    print("\n▶️ Pump resumed via physical button.")
                    _set_status(f"💧 Dispensing Water... ({shared_state.ml_to_dispense:.0f}mL Target)", "#059669")

                # 4. DISPENSING PROGRESS & FINAL VOLUME RECEIVER
                elif shared_state.is_flow_monitoring_mode:

                    if hardware_data.startswith("DISPENSED_FINAL_ML:"):
                        try:
                            final_ml_val = float(hardware_data.split(":")[1].strip())
                            shared_state.actual_dispensed_ml = round(final_ml_val, 1)
                            print(f"\n⏱ Final Volume Received: {shared_state.actual_dispensed_ml} mL")
                        except Exception as e:
                            print(f"⚠️ Error parsing final ml: {e}")

                    elif hardware_data.startswith("DISPENSING_PROGRESS_ML:"):
                        try:
                            progress_part = hardware_data.split(":")[1].strip()
                            dispensed_ml_str, target_ml_str = progress_part.split("/")

                            dispensed_val = float(dispensed_ml_str)
                            target_val = float(target_ml_str)

                            # I-save habang nagpo-progress
                            shared_state.actual_dispensed_ml = round(dispensed_val, 1)

                            percent = min(100, round((dispensed_val / max(1.0, target_val)) * 100))
                            
                            if not getattr(shared_state, 'is_pump_paused', False):
                                status_txt = f"💧 Dispensing: {percent}% ({shared_state.actual_dispensed_ml}mL / {shared_state.ml_to_dispense:.0f}mL)"
                                print(f"\r{status_txt}", end="")
                                _set_status(status_txt, "#059669")
                        except (IndexError, ValueError):
                            pass

                    # KAGANAPAN KAPAG NATAPOS O NAG-TIMEOUT SA PAUSE
                    elif "TARGET_REACHED" in hardware_data or "PUMP_OFF" in hardware_data or "PAUSE_TIMEOUT" in hardware_data:
                        print(f"\n✅ Dispensing terminated ({hardware_data}). Current calculated volume: {shared_state.actual_dispensed_ml} mL")
                        
                        # Bigyan ng konting delay para makuha kung may huling DISPENSED_FINAL_ML na darating
                        time.sleep(0.3)
                        
                        shared_state.is_flow_monitoring_mode = False
                        shared_state.is_pump_paused = False

            time.sleep(0.02)

    threading.Thread(target=hardware_listener_loop, daemon=True).start()


def _startup_worker():
    """
    Tumatakbo sa background para hindi ma-freeze ang GUI.
    """
    time.sleep(1.5)
    _set_status("⏳ Connecting to cloud...", "#F59E0B")

    connect_to_esp32()
    threading.Thread(target=_esp32_watchdog, daemon=True).start()

    attempt = 0
    while True:
        attempt += 1
        if firebase_handler.initialize_firebase_system():
            break
        print(f"❌ Firebase Initialization Failed (attempt {attempt}). Retrying in {FIREBASE_RETRY_DELAY}s...")
        _set_status("⚠️ No cloud connection - retrying...", "#e74c3c")
        time.sleep(FIREBASE_RETRY_DELAY)

    _start_hardware_listener()
    print("🚀 Vending Machine Service Active & Listening...")
    _set_status("⏳ Ready to Scan QR Code", "#2ecc71")


def start_h2o_core_system():
    print("\n--- H2O HUB: SMART SYSTEM RUNNING ---")
    threading.Thread(target=_startup_worker, daemon=True).start()
    return True