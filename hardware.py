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

    # 📦 KUNIN ANG PENDING WATER AT CLAIM STATUS MULA SA FIREBASE
    pending_water = float(user_data.get('pending_ml', 0.0) or 0.0)
    claim_pending = user_data.get('claim_pending', False)

    shared_state.active_student_uid = scanned_uid
    
    # I-set muna sa True ang is_scanning para sa UI ng app
    user_ref.update({
        'coin_trigger': False, 
        'is_scanning': True, 
        'is_dispensing': False,
        'last_credits': 0
    })

    if pending_water > 0 and claim_pending:
        welcome_msg = f"👋 Welcome, {user_data.get('name', 'Student')}! (May {pending_water}mL pending water)"
        print(f"💡 [PENDING DETECTED]: Mayroon kang {pending_water} mL. Naghihintay sa pagpindot ng Claim button...")
    else:
        welcome_msg = f"👋 Welcome, {user_data.get('name', 'Student')}!"
        print(welcome_msg)

    _set_status(welcome_msg, "#3498db")
    time.sleep(1.0)
    
    # Tapos na ang scanning state, lilitaw na ang buttons sa app
    user_ref.update({'is_scanning': False})
    print("is_scanning False. Waiting for Claim or Coins...")

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

    print(f"🪙 Waiting for coins or claim for user: {scanned_uid}...")

    # 1. COIN & CLAIM ACCUMULATION LOOP (Dito aabangan ang Claim o Barya)
    claimed_water = 0.0
    is_claimed_now = False

    while shared_state.is_coin_accumulation_mode:
        try:
            if shared_state.vendo_ref.child('force_dispense').get() is True:
                shared_state.coin_amount = 0
                shared_state.is_coin_accumulation_mode = False
                break
        except Exception:
            pass

        # 🔍 SURIIN KUNG PININDOT NA NG USER ANG CLAIM BUTTON SA APP
        current_status = user_ref.get() or {}
        
        # Kapag nag-signal ang app na pinindot ang claim ('is_claiming': True)
        app_claiming = current_status.get('is_claiming', False)
        
        if app_claiming and pending_water > 0 and not is_claimed_now:
            claimed_water = pending_water
            shared_state.current_user_pending_water = claimed_water
            is_claimed_now = True
            
            print(f"📦 [CLAIM BUTTON PRESSED]: Successfully claimed {claimed_water} mL!")
            
            # Ngayon pa lang natin buburahin sa Firebase para mawala ang Claim button at lumabas ang Dispense
            user_ref.update({
                'pending_ml': 0.0,
                'claim_pending': False,
                'is_claiming': False,
                'coin_trigger': True
            })
            
            _set_status(f"📦 Claimed {claimed_water}mL! Maghulog ng barya o i-tap ang Dispense.", "#2ecc71")
            time.sleep(0.5)

        # Timeout sa barya kung may hinulog
        if shared_state.coin_amount > 0 and (time.time() - shared_state.last_coin_time > shared_state.timeout_duration):
            print(f"\n⏳ Coin insertion timeout. Total accumulated: ₱{shared_state.coin_amount}")
            shared_state.is_coin_accumulation_mode = False
            break

        # Kiosk Session Timeout (40 seconds na walang barya at hindi nag-claim)
        if shared_state.coin_amount == 0 and not is_claimed_now and (time.time() - shared_state.last_coin_time > 40.0):
            print(f"\n⏳ Kiosk Session Timeout. Walang barya at hindi nag-claim.")
            if shared_state.esp32:
                try:
                    with shared_state.serial_lock:
                        shared_state.esp32.write(b'TIMEOUT_RESET\n')
                except Exception as e:
                    print(f"⚠️ Serial Write Error (TIMEOUT_RESET): {e}")
            shared_state.is_coin_accumulation_mode = False
            break

        # Kung may hinulog na barya O kaya ay na-claim na, pwede nang mag-break para tumuloy sa dispense loop
        if shared_state.coin_amount > 0 or is_claimed_now:
            # Bibigyan natin ng konting sandali kung sakaling maghuhulog pa ng barya pagkatapos mag-claim,
            # pero kung tapos na ang paghihintay ng barya, mag-break na paglipas ng ilang segundo o kapag pinindot ang dispense.
            pass

        # Kung direktang pinindot na ang Dispense button sa app habang nasa accumulation loop
        if current_status.get('is_dispensing') == True:
            shared_state.is_coin_accumulation_mode = False
            break

        time.sleep(0.2)

    # Kalkulahin ang kabuuang ML (Barya + Na-claim na Pending Water)
    coin_ml = float(shared_state.coin_amount) * float(shared_state.LIVE_ML_PER_PESO)
    shared_state.ml_to_dispense = coin_ml + claimed_water

    # Suriin kung walang barya at walang na-claim na tubig
    if shared_state.coin_amount == 0 and claimed_water <= 0:
        print("⏳ Walang barya at walang na-claim na tubig. Kina-cancel ang session.")
        shared_state.active_student_uid = None
        _set_status("⏳ Ready to Scan QR Code", "#2ecc71")
        return

    user_ref.update({
        'last_credits': shared_state.coin_amount, 
        'target_volume': shared_state.ml_to_dispense,
        'is_scanning': False, 
        'coin_trigger': True
    })

    _set_status(
        f"🪙 Final Total: ₱{shared_state.coin_amount} ({shared_state.ml_to_dispense:.0f}mL). Tap Dispense on Phone!",
        "#f1c40f"
    )

    print(f"📱 Ready! Coins: ₱{shared_state.coin_amount} ({coin_ml}mL) + Claimed Water: {claimed_water}mL = Total: {shared_state.ml_to_dispense}mL")
    print("📱 Waiting for Mobile App 'Dispense' click...")

    # 2. DISPENSE TRIGGER WAIT LOOP
    dispense_started = False

    # === CONFIGURATION AT INITIALIZATION ===
    IDLE_TIMEOUT_SECONDS = 30.0   # 30 seconds idle timeout bago i-release ang kiosk
    ML_PER_PESO = 100            # Fixed rate: 100 mL bawat ₱1
    
    wait_start_time = time.time()
    previous_credits = 0          # In-initialize sa labas para iwas UnboundLocalError

    while True:
        try:
            if shared_state.vendo_ref.child('force_dispense').get() is True:
                break

            current_status = user_ref.get() or {}

            # ----------------------------------------------------
            # A. KAPAG NAG-TRIGGER NA NG DISPENSE (COIN / BUTTON)
            # ----------------------------------------------------
            if current_status.get('is_dispensing') == True:

                user_ref.update({'coin_trigger': False, 'is_dispensing': False})
                
                dispense_started = True
                shared_state.actual_dispensed_ml = 0.0

                # ========================================================
                # 1. BASE ENTITLEMENT (Barya + Na-claim na Pending Water)
                # ========================================================
                inserted_coins = float(current_status.get('last_credits', 0) or 0)
                base_coin_ml = inserted_coins * ML_PER_PESO  # ₱1 -> 100mL, ₱2 -> 200mL, ₱5 -> 500mL
                
                # Condition para sa Pagsasama ng Barya at Claimed Water
                if inserted_coins > 0 and claimed_water > 0:
                    target_dispense_ml = base_coin_ml + claimed_water
                elif claimed_water > 0:
                    target_dispense_ml = claimed_water
                else:
                    target_dispense_ml = base_coin_ml
                
                # Set ang ipapakitang volume sa Mobile App at Logging
                shared_state.ml_to_dispense = target_dispense_ml

                # Update agad sa Firebase para makita ng user sa App
                user_ref.update({
                    'target_volume': target_dispense_ml,
                    'dispensing_status': 'processing'
                })

                # ========================================================
                # 2. HARDWARE / ESP32 MULTIPLIER CONDITION
                # ========================================================
                try:
                    settings_ref = shared_state.vendo_ref.child('settings').get() or {}
                    web_calibration = float(settings_ref.get('ml_per_peso', 100))
                except Exception as e:
                    print(f"⚠️ Error reading calibration: {e}")
                    web_calibration = 100.0

                calibration_factor = web_calibration / 100.0
                esp32_target_ml = target_dispense_ml * calibration_factor

                # TRIGGER ESP32 PUMP
                if shared_state.esp32:
                    shared_state.is_flow_monitoring_mode = True
                    shared_state.is_pump_paused = False
                    shared_state.paused_time_offset = 0.0

                    command_to_send = f"START_PUMP_ML:{esp32_target_ml:.1f}\n"

                    with shared_state.serial_lock:
                        shared_state.esp32.reset_input_buffer()
                        shared_state.esp32.write(command_to_send.encode())

                    print(f"📡 [SERIAL SENT TO ESP32]: {command_to_send.strip()} (Target Total: {target_dispense_ml} mL)")

                start_flow_time = time.time()

                # 3. DISPENSING PROGRESS MONITORING LOOP
                while shared_state.is_flow_monitoring_mode:
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
                            
                            wait_start = time.time()
                            while shared_state.is_flow_monitoring_mode and (time.time() - wait_start) < 3.0:
                                time.sleep(0.05)
                            shared_state.is_flow_monitoring_mode = False
                            break

                    time.sleep(0.05)

                time.sleep(0.5)

                # 4. VALIDATION AT UPDATE NG NAIBUHOS NA TUBIG (PARTIAL / FULL)
                poured_ml = getattr(shared_state, 'actual_dispensed_ml', 0.0)
                poured_ml = min(poured_ml, float(shared_state.ml_to_dispense))
                poured_ml = round(float(poured_ml), 1)

                print(f"\n📊 FINAL DISPENSED AMOUNT: {poured_ml} mL (Target was {shared_state.ml_to_dispense} mL)")

                # Kalkulahin ang natirang tubig (Remainder para sa pending_ml)
                remainder_ml = round(float(shared_state.ml_to_dispense) - poured_ml, 1)

                if poured_ml <= 0:
                    print("⚠️ 0 mL dispensed. Pinabalik ang buong target sa pending_ml.")
                    user_ref.update({
                        'is_scanning': False,
                        'coin_trigger': False,
                        'last_credits': 0,
                        'pending_ml': remainder_ml,
                        'claim_pending': True if remainder_ml > 0 else False
                    })
                    break

                # Update gallon water level
                shared_state.current_water_level = max(0, shared_state.current_water_level - int(poured_ml))
                water_percentage = round((shared_state.current_water_level / _water_capacity()) * 100)

                try:
                    shared_state.vendo_ref.update({'water_level': water_percentage})
                    print(f"📉 GALLON UPDATE: {shared_state.current_water_level} mL remaining ({water_percentage}%)")
                except Exception as e:
                    print(f"⚠️ Water level push error: {e}")

                # Accumulate current intake ng user
                current_user_data = user_ref.get() or {}
                raw_intake = current_user_data.get('intake', 0) or 0
                if isinstance(raw_intake, str):
                    raw_intake = raw_intake.replace('M', '').replace('m', '').strip()

                old_intake = float(raw_intake)
                new_intake = round(old_intake + poured_ml, 1)
                finish_time = time.strftime("%Y-%m-%d %H:%M:%S")

                # I-update ang Firebase (isama ang remainder sa pending_ml kung may natira)
                user_ref.update({
                    'intake': new_intake,
                    'last_drink_time': finish_time,
                    'is_scanning': False,
                    'coin_trigger': False,
                    'last_credits': 0,
                    'pending_ml': remainder_ml,
                    'claim_pending': True if remainder_ml > 5.0 else False
                })

                if remainder_ml > 5.0:
                    print(f"📦 [PENDING SAVED]: May natirang {remainder_ml} mL na inimbak sa Firebase bilang pending_ml.")
                else:
                    print("✅ Naubos nang buo ang target volume!")

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
            # B. WALA PANG DISPENSE: IDLE TIMEOUT MONITORING
            # ----------------------------------------------------
            else:
                current_credits = float(current_status.get('last_credits', 0) or 0)

                if current_credits > previous_credits:
                    print(f"🪙 [COIN DETECTED]: Credits updated to {current_credits}. Resetting idle timer...")
                    wait_start_time = time.time()
                    previous_credits = current_credits

                if (time.time() - wait_start_time) > IDLE_TIMEOUT_SECONDS:
                    print(f"\n⏰ [IDLE TIMEOUT] Lumagpas sa {IDLE_TIMEOUT_SECONDS:.0f}s na walang aksyon. Ibabalik sa pending_ml ang barya...")

                    timeout_coins_ml = current_credits * ML_PER_PESO
                    total_saved_pending = timeout_coins_ml + claimed_water

                    user_ref.update({
                        'is_scanning': False,
                        'coin_trigger': False,
                        'last_credits': 0,
                        'pending_ml': total_saved_pending,
                        'claim_pending': True if total_saved_pending > 0 else False
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

                elif hardware_data.startswith("UID_"):
                    uid = hardware_data.replace("UID_", "").strip()
                    if shared_state.active_student_uid is not None:
                        print(f"⚠️ KIOSK BUSY: Tinanggihan si {uid}.")
                        _set_status("⚠️ System Busy!", "#e74c3c")
                        continue
                    threading.Thread(target=process_scanned_student, args=(uid,), daemon=True).start()

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

                elif shared_state.is_flow_monitoring_mode:

                    if hardware_data.startswith("DISPENSED_FINAL_ML:"):
                        try:
                            final_ml_val = float(hardware_data.split(":")[1].strip())
                            shared_state.actual_dispensed_ml = int(round(final_ml_val))
                            print(f"\n⏱ Final Volume Received: {final_ml_val:.1f} mL")
                        except Exception as e:
                            print(f"⚠️ Error parsing final ml: {e}")

                    elif hardware_data.startswith("DISPENSING_PROGRESS_ML:"):
                        try:
                            progress_part = hardware_data.split(":")[1].strip()
                            dispensed_ml_str, target_ml_str = progress_part.split("/")

                            dispensed_val = float(dispensed_ml_str)
                            target_val = float(target_ml_str)

                            shared_state.actual_dispensed_ml = int(round(dispensed_val))

                            percent = min(100, round((dispensed_val / max(1.0, target_val)) * 100))
                            
                            if not getattr(shared_state, 'is_pump_paused', False):
                                status_txt = f"💧 Dispensing: {percent}% ({shared_state.actual_dispensed_ml}mL / {shared_state.ml_to_dispense:.0f}mL)"
                                print(f"\r{status_txt}", end="")
                                _set_status(status_txt, "#059669")
                        except (IndexError, ValueError):
                            pass

                    elif "TARGET_REACHED" in hardware_data or "PUMP_OFF" in hardware_data or "PAUSE_TIMEOUT" in hardware_data:
                        print(f"\n✅ Dispensing terminated ({hardware_data}). Final calculated volume: {shared_state.actual_dispensed_ml} mL")
                        shared_state.is_flow_monitoring_mode = False
                        shared_state.is_pump_paused = False

            time.sleep(0.02)

    threading.Thread(target=hardware_listener_loop, daemon=True).start()


def _startup_worker():
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