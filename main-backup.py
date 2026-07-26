import firebase_admin
from firebase_admin import credentials, db
import time
import serial
import threading
import sys 
import json
import os
import customtkinter as ctk
import pywifi  

# --- 1. GLOBAL CONFIGURATION FOR GUI & SYSTEM ---
SERIAL_PORT = '/dev/ttyACM0'  # Inayon sa totoong port ng ESP32-S3 mo
BAUD_RATE = 115200
CONFIG_FILE = "config.json" 

VENDO_ID = "vendo_004" 
VENDO_NAME = "Lobby Dispenser 1"

current_water_level = 85 
active_student_uid = None  
esp32 = None

ctk.set_appearance_mode("Dark")  
ctk.set_default_color_theme("blue")

# --- 2. CONFIGURATION PERSISTENCE STORAGE LOGIC ---
def save_config_to_local(connection_type, name_id, name_public, wifi_ssid="", wifi_pass=""):
    config_data = {
        "connection_type": connection_type,
        "vendo_id": name_id,
        "vendo_name": name_public,
        "wifi_ssid": wifi_ssid,
        "wifi_password": wifi_pass
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

# --- 3. WI-FI SCANNING LOGIC ---
def get_real_wifi_networks():
    try:
        wifi = pywifi.PyWiFi()
        iface = wifi.interfaces()[0]
        iface.scan()
        time.sleep(2)
        scan_results = iface.scan_results()
        wifi_list = []
        for network in scan_results:
            ssid = network.ssid.strip()
            if ssid and ssid not in wifi_list:
                wifi_list.append(ssid)
        if not wifi_list:
            return ["Walang nasagap na Wi-Fi (Check Hardware)"]
        return sorted(wifi_list)
    except Exception as e:
        print(f"⚠️ Wi-Fi Scanner Error: {e}")
        return ["Hindi mabasa ang Wi-Fi Card"]

# --- 4. FIREBASE INITIALIZATION & HEARTBEAT FUNCTION ---
def initialize_firebase_system():
    global vendo_ref
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate("key.json")
            firebase_admin.initialize_app(cred, {
                'databaseURL': 'https://h2o-project-e83d9-default-rtdb.firebaseio.com'
            })
        vendo_ref = db.reference(f'vendos/{VENDO_ID}')
        
        # Simulan ang background heartbeat thread para laging "Connected" ang status sa DB
        threading.Thread(target=start_heartbeat_loop, daemon=True).start()
        return True
    except Exception as e:
        print(f"❌ Firebase Connection Error: {e}")
        return False

def update_vending_status(status, water_level):
    if 'vendo_ref' in globals():
        try:
            vendo_ref.update({
                'vendo_name': VENDO_NAME,
                'wifi_status': status,
                'water_level': water_level,
                'last_online': time.strftime("%Y-%m-%d %H:%M:%S")
            })
        except Exception as e:
            print(f"⚠️ Failed to update firebase status: {e}")

def start_heartbeat_loop():
    """Tatakbo ito habang buhay ang app para mag-ping sa Firebase bawat 5 segundo"""
    print("💓 Heartbeat monitor initialized.")
    while True:
        update_vending_status("Connected", current_water_level)
        time.sleep(5) # Mag-a-update tuwing 5 segundo

def listen_for_admin_commands():
    global active_student_uid
    def listener(event):
        if event.data is True:
            print("\n🚀 ADMIN COMMAND: Force Dispensing Water...")
            if esp32: esp32.write(b'START_PUMP\n')
            
            for remaining in range(5, 0, -1):
                sys.stdout.write(f"\r⏳ Dispensing... {remaining}s left")
                sys.stdout.flush()
                time.sleep(1)
            print("\r✅ Dispensing... Done!          ")
            if esp32: esp32.write(b'STOP_PUMP\n')
            
            if active_student_uid:
                db.reference(f'users/{active_student_uid}').update({
                    'coin_trigger': False, 'is_scanning': False, 'last_credits': 0
                })
            vendo_ref.update({'force_dispense': False}) 

    vendo_ref.child('force_dispense').listen(listener)

# --- 5. MAIN CORE SYSTEM LOGIC ---
def start_h2o_core_system():
    global current_water_level, active_student_uid, esp32
    print("\n--- H2O HUB: SMART SYSTEM RUNNING ---")
    
    try:
        esp32 = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        print(f"✅ Hardware Linked: {SERIAL_PORT}")
    except Exception as e:
        print(f"⚠️ Hardware Offline: {e}")
        esp32 = None

    if not initialize_firebase_system():
        print("🚨 System cannot run without Firebase Initialization.")
        return

    threading.Thread(target=listen_for_admin_commands, daemon=True).start()

    COIN_CONFIG = {
        1:  {"seconds": 3.5,  "ml": 250},
        5:  {"seconds": 5.5,  "ml": 500},
        10: {"seconds": 8.5,  "ml": 750},
        20: {"seconds": 17.0, "ml": 1000}
    }

    def core_loop():
        global current_water_level, active_student_uid
        while True:
            active_student_uid = None
            scanned_uid = input("\n[SCANNER] Scan Student UID: ").strip()
            if not scanned_uid: continue

            user_ref = db.reference(f'users/{scanned_uid}')
            user_data = user_ref.get()
            if not user_data:
                print(f"❌ User {scanned_uid} not found.")
                continue

            active_student_uid = scanned_uid
            user_ref.update({'coin_trigger': False, 'is_scanning': False, 'last_credits': 0})
            print(f"👋 Welcome, {user_data.get('name', 'Student')}!")
            
            if esp32: 
                esp32.reset_input_buffer()
                esp32.write(b'READY_FOR_COINS\n')

            amount = 0
            while True:
                if vendo_ref.child('force_dispense').get() is True:
                    amount = 0
                    break
                if esp32 and esp32.in_waiting > 0:
                    msg = esp32.readline().decode('utf-8').strip()
                    if msg == "1_PESO_DETECTED": amount = 1; break
                    elif msg == "5_PESOS_DETECTED": amount = 5; break
                    elif msg == "10_PESOS_DETECTED": amount = 10; break
                    elif msg == "20_PESOS_DETECTED": amount = 20; break
                time.sleep(0.1)

            if amount == 0: continue

            if amount in COIN_CONFIG:
                dispense_time = COIN_CONFIG[amount]["seconds"]
                ml_to_dispense = COIN_CONFIG[amount]["ml"]
                user_ref.update({'last_credits': amount, 'is_scanning': True})

                is_forced = False
                while True:
                    if vendo_ref.child('force_dispense').get() is True:
                        is_forced = True
                        break
                    current_status = user_ref.get()
                    if current_status and current_status.get('coin_trigger') == True:
                        if esp32: esp32.write(b'START_PUMP\n')
                        time_left = dispense_time
                        while time_left > 0:
                            sys.stdout.write(f"\r⏳ Pouring... {time_left:.1f}s remaining")
                            sys.stdout.flush()
                            time.sleep(0.5)
                            time_left -= 0.5
                        print("\r✅ Pouring... Complete!                                 ") 
                        if esp32: esp32.write(b'STOP_PUMP\n')
                        
                        current_water_level = max(0, current_water_level - 1)
                        new_intake = (current_status.get('intake', 0) or 0) + ml_to_dispense
                        finish_time = time.strftime("%Y-%m-%d %H:%M:%S")
                        
                        user_ref.update({
                            'intake': new_intake, 'last_drink_time': finish_time,
                            'is_scanning': False, 'coin_trigger': False, 'last_credits': 0
                        })
                        db.reference('dispense_logs').push({
                            'uid': scanned_uid, 'name': user_data.get('name', 'Unknown'),
                            'vendo_id': VENDO_ID, 'amount_ml': ml_to_dispense, 'timestamp': finish_time, 'status': "Success"
                        })
                        break
                    time.sleep(0.5)

    threading.Thread(target=core_loop, daemon=True).start()

# --- 6. THE KIOSK GUI INTERFACE SETUP ---
class H2OHubKioskSetup(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("H2O HUB - Smart Setup Console")
        self.geometry("600x550")
        self.resizable(False, False)

        # Intercept ang close button (X) para mapilitang mag-offline ang status bago sumara
        self.protocol("WM_DELETE_WINDOW", self.on_closing_app)

        self.main_frame = ctk.CTkFrame(self)
        self.main_frame.pack(fill="both", expand=True, padx=20, pady=20)

        self.saved_config = load_local_config()
        if self.saved_config:
            self.auto_deploy_existing_hardware()
        else:
            self.show_welcome_screen()

    def clear_frame(self):
        for widget in self.main_frame.winfo_children():
            widget.destroy()

    def show_welcome_screen(self):
        self.clear_frame()
        title_label = ctk.CTkLabel(self.main_frame, text="H2O HUB KIOSK SETUP", font=ctk.CTkFont(size=24, weight="bold"))
        title_label.pack(pady=40)

        wifi_btn = ctk.CTkButton(self.main_frame, text="📶 SETUP WI-FI CONNECTION", width=300, height=50, font=ctk.CTkFont(size=14, weight="bold"), command=self.show_wifi_setup)
        wifi_btn.pack(pady=15)

        lan_btn = ctk.CTkButton(self.main_frame, text="🔌 USE LAN PORT CONNECTION", width=300, height=50, fg_color="#2b2b2b", hover_color="#3a3a3a", font=ctk.CTkFont(size=14, weight="bold"), command=self.process_lan_setup)
        lan_btn.pack(pady=15)

    def show_wifi_setup(self):
        self.clear_frame()
        header_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        header_frame.pack(fill="x", pady=10)

        back_btn = ctk.CTkButton(header_frame, text="⬅ Back", width=70, fg_color="#c0392b", hover_color="#e74c3c", command=self.show_welcome_screen)
        back_btn.pack(side="left", padx=10)

        ctk.CTkLabel(self.main_frame, text="Select Campus Network:").pack(pady=(15, 2))
        
        status_msg = ctk.CTkLabel(self.main_frame, text="Scanning networks, please wait...", text_color="#f1c40f")
        status_msg.pack()
        self.update()
        
        real_networks = get_real_wifi_networks()
        status_msg.destroy()
        
        self.wifi_dropdown = ctk.CTkComboBox(self.main_frame, values=real_networks, width=350)
        self.wifi_dropdown.pack(pady=5)

        ctk.CTkLabel(self.main_frame, text="Enter Wi-Fi Password:").pack(pady=(10, 2))
        self.wifi_password_entry = ctk.CTkEntry(self.main_frame, placeholder_text="••••••••", show="*", width=350)
        self.wifi_password_entry.pack(pady=5)

        ctk.CTkLabel(self.main_frame, text="Vendo Public Name:").pack(pady=(10, 2))
        self.vendo_name_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Hal. Campus Ground Floor Vendo", width=350)
        self.vendo_name_entry.insert(0, "Lobby Dispenser 1")
        self.vendo_name_entry.pack(pady=5)

        ctk.CTkLabel(self.main_frame, text="Vendo System ID:").pack(pady=(10, 2))
        self.vendo_id_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Hal. vendo_004", width=350)
        self.vendo_id_entry.insert(0, "vendo_004")
        self.vendo_id_entry.pack(pady=5)

        self.save_btn = ctk.CTkButton(self.main_frame, text="💾 SAVE & ACTIVATE VENDO", width=250, height=45, fg_color="#27ae60", hover_color="#2ecc71", font=ctk.CTkFont(weight="bold"), command=self.save_and_deploy_wifi)
        self.save_btn.pack(pady=30)

    def process_lan_setup(self):
        self.clear_frame()
        header_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        header_frame.pack(fill="x", pady=10)

        back_btn = ctk.CTkButton(header_frame, text="⬅ Back", width=70, fg_color="#c0392b", hover_color="#e74c3c", command=self.show_welcome_screen)
        back_btn.pack(side="left", padx=10)

        ctk.CTkLabel(self.main_frame, text="Vendo Public Name:").pack(pady=(10, 2))
        self.vendo_name_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Hal. Campus Ground Floor Vendo", width=350)
        self.vendo_name_entry.insert(0, "Lobby Dispenser 1")
        self.vendo_name_entry.pack(pady=5)

        ctk.CTkLabel(self.main_frame, text="Vendo System ID:").pack(pady=(10, 2))
        self.vendo_id_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Hal. vendo_004", width=350)
        self.vendo_id_entry.insert(0, "vendo_004")
        self.vendo_id_entry.pack(pady=5)

        self.save_btn = ctk.CTkButton(self.main_frame, text="💾 SAVE & ACTIVATE VENDO", width=250, height=45, fg_color="#27ae60", hover_color="#2ecc71", font=ctk.CTkFont(weight="bold"), command=self.save_and_deploy_lan)
        self.save_btn.pack(pady=30)

    def save_and_deploy_wifi(self):
        global VENDO_ID, VENDO_NAME
        selected_net = self.wifi_dropdown.get()
        net_pass = self.wifi_password_entry.get()
        VENDO_NAME = self.vendo_name_entry.get()
        VENDO_ID = self.vendo_id_entry.get()

        save_config_to_local("WIFI", VENDO_ID, VENDO_NAME, selected_net, net_pass)
        self.transition_to_running_state()

    def save_and_deploy_lan(self):
        global VENDO_ID, VENDO_NAME
        VENDO_NAME = self.vendo_name_entry.get()
        VENDO_ID = self.vendo_id_entry.get()

        save_config_to_local("LAN", VENDO_ID, VENDO_NAME)
        self.transition_to_running_state()

    def auto_deploy_existing_hardware(self):
        global VENDO_ID, VENDO_NAME
        VENDO_ID = self.saved_config["vendo_id"]
        VENDO_NAME = self.saved_config["vendo_name"]
        self.transition_to_running_state()

    def transition_to_running_state(self):
        self.clear_frame()
        status_lbl = ctk.CTkLabel(self.main_frame, text="🚀 VENDO LIVE & ACTIVE", font=ctk.CTkFont(size=22, weight="bold"), text_color="#2ecc71")
        status_lbl.pack(pady=40)

        info_lbl = ctk.CTkLabel(self.main_frame, text=f"ID: {VENDO_ID}\nName: {VENDO_NAME}\n\nRunning operational algorithms...", font=ctk.CTkFont(size=14))
        info_lbl.pack(pady=10)

        reset_btn = ctk.CTkButton(self.main_frame, text="⚙️ RESET HARDWARE CONFIG", fg_color="#c0392b", hover_color="#e74c3c", command=self.confirm_hardware_reset)
        reset_btn.pack(pady=50)

        start_h2o_core_system()

    def confirm_hardware_reset(self):
        self.confirm_win = ctk.CTkToplevel(self)
        self.confirm_win.title("Confirmation Box")
        self.confirm_win.geometry("380x180")
        self.confirm_win.resizable(False, False)
        self.confirm_win.attributes("-topmost", True) 

        msg_lbl = ctk.CTkLabel(self.confirm_win, text="Do you want to reset hardware configuration?", font=ctk.CTkFont(size=14, weight="bold"))
        msg_lbl.pack(pady=30)

        btn_frame = ctk.CTkFrame(self.confirm_win, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20)

        yes_btn = ctk.CTkButton(btn_frame, text="Yes", fg_color="#27ae60", hover_color="#2ecc71", width=120, command=self.execute_hardware_wipe)
        yes_btn.pack(side="left", padx=15)

        no_btn = ctk.CTkButton(btn_frame, text="No", fg_color="#7f8c8d", hover_color="#95a5a6", width=120, command=self.confirm_win.destroy)
        no_btn.pack(side="right", padx=15)

    def execute_hardware_wipe(self):
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
        
        # Piliting gawing offline sa DB kapag nag-reset ng connection
        if 'vendo_ref' in globals():
            vendo_ref.update({'wifi_status': 'Offline'})
            
        self.confirm_win.destroy()
        self.show_welcome_screen()

    def on_closing_app(self):
        """Tatakbo ito kapag pinindot ang 'X' sa kanang itaas ng bintana"""
        print("\n🧹 Cleaning up cloud states... Setting Vendo to Offline.")
        try:
            if 'vendo_ref' in globals():
                # Bago tuluyang mamatay ang app, baguhin ang status sa Firebase
                vendo_ref.update({'wifi_status': 'Offline'})
        except Exception:
            pass
        if esp32: 
            esp32.write(b'STOP_PUMP\n')
        self.destroy()
        sys.exit(0)

if __name__ == "__main__":
    try:
        app = H2OHubKioskSetup()
        app.mainloop()
    except KeyboardInterrupt:
        if 'vendo_ref' in globals():
            vendo_ref.update({'wifi_status': 'Offline'})
        if esp32: esp32.write(b'STOP_PUMP\n')
        print("\nSystem Shutting Down. Bye!")

