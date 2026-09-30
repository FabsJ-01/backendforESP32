import sys 
import os
import socket
import threading

# --- 1. SINGLE INSTANCE LOCK (PIGILAN ANG PAGDODOBLE NG APP) ---
try:
    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lock_socket.bind(('127.0.0.1', 65432))
except socket.error:
    print("⚠️ Naka-run na ang H2O HUB Kiosk App! Isasara ang bagong instance.")
    sys.exit(0)

import customtkinter as ctk
import shared_state
import hardware

# --- PINALITAN ANG THEME AT APPEARANCE MODE PARA SA WHITE BACKGROUND ---
ctk.set_appearance_mode("Light")  
ctk.set_default_color_theme("blue")

class H2OHubKioskSetup(ctk.CTk):
    def __init__(self):
        super().__init__()
        shared_state.app_instance = self

        self.title("H2O HUB - Smart Setup Console")
        
        # Protocol handler para sa pag-close ng app
        self.protocol("WM_DELETE_WINDOW", self.on_closing_app)
        
        # Pwersahang White ang Root Window Background
        self.configure(fg_color="#FFFFFF")
        
        # --- AUTOMATIC FULLSCREEN AT WINDOW PRIORITY SETUP ---
        self.attributes("-fullscreen", True)
        self.attributes("-topmost", True)  # Pwersahing laging nasa ibabaw para hindi mawalan ng focus
        
        # Fallback bindings para makalabas sa fullscreen kung kailangan ng admin (Esc key)
        self.bind("<Escape>", lambda event: self.attributes("-fullscreen", False))

        # --- RESPONSIVE STRUCTURAL LAYOUT LOGIC ---
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Ang Main Frame ay ginawang parang Card Panel na lumulutang sa gitna ng Screen
        self.main_frame = ctk.CTkFrame(self, fg_color="#F5F6FA", corner_radius=20, border_width=1, border_color="#E2E8F0")
        self.main_frame.grid(row=0, column=0, padx=60, pady=60, sticky="nsew")

        self.saved_config = shared_state.load_local_config()
        if self.saved_config:
            self.auto_deploy_existing_hardware()
        else:
            self.show_welcome_screen()

    def clear_frame(self):
        for widget in self.main_frame.winfo_children():
            widget.destroy()

    def show_welcome_screen(self):
        # Siguraduhing tatanggalin muna ang anumang FocusIn bindings kapag bumalik sa Welcome/Setup Screen
        try:
            self.unbind("<FocusIn>")
        except Exception:
            pass

        self.clear_frame()
        
        # Pag-setup ng Responsive Grid Alignment sa Loob ng Main Container
        self.main_frame.grid_columnconfigure(0, weight=1)
        for r in range(6):
            self.main_frame.grid_rowconfigure(r, weight=1)
        
        title_label = ctk.CTkLabel(self.main_frame, text="H2O HUB KIOSK SETUP", font=ctk.CTkFont(size=36, weight="bold"), text_color="#1E293B")
        title_label.grid(row=0, column=0, pady=(40, 20), sticky="s")

        ctk.CTkLabel(self.main_frame, text="Vendo Public Name:", font=ctk.CTkFont(size=16, weight="bold"), text_color="#475569").grid(row=1, column=0, pady=(10, 2), sticky="s")
        self.vendo_name_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Hal. Campus Ground Floor Vendo", width=500, height=45, font=ctk.CTkFont(size=14), fg_color="#FFFFFF", text_color="#0F172A", border_color="#CBD5E1")
        self.vendo_name_entry.insert(0, "Lobby Dispenser 1")
        self.vendo_name_entry.grid(row=2, column=0, pady=5, sticky="n")

        ctk.CTkLabel(self.main_frame, text="Vendo System ID:", font=ctk.CTkFont(size=16, weight="bold"), text_color="#475569").grid(row=3, column=0, pady=(10, 2), sticky="s")
        self.vendo_id_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Hal. vendo_004", width=500, height=45, font=ctk.CTkFont(size=14), fg_color="#FFFFFF", text_color="#0F172A", border_color="#CBD5E1")
        self.vendo_id_entry.insert(0, "vendo_004")
        self.vendo_id_entry.grid(row=4, column=0, pady=5, sticky="n")

        self.save_btn = ctk.CTkButton(self.main_frame, text="🚀 SAVE & ACTIVATE KIOSK", width=350, height=60, fg_color="#10B981", hover_color="#059669", font=ctk.CTkFont(size=16, weight="bold"), text_color="#FFFFFF", corner_radius=12)
        self.save_btn.configure(command=self.save_and_deploy)
        self.save_btn.grid(row=5, column=0, pady=(30, 40), sticky="n")

        self.after(300, lambda: self.vendo_name_entry.focus_set())

    def save_and_deploy(self):
        v_name = self.vendo_name_entry.get().strip()
        v_id = self.vendo_id_entry.get().strip()
        
        if v_name and v_id:
            shared_state.save_config_to_local(v_id, v_name)
            self.transition_to_running_state()
        else:
            print("⚠️ Error: Huwag iwang bakante ang Vendo Name at System ID.")

    def auto_deploy_existing_hardware(self):
        shared_state.VENDO_ID = self.saved_config["vendo_id"]
        shared_state.VENDO_NAME = self.saved_config["vendo_name"]
        print(f"📦 Auto-loaded persistent config: ID={shared_state.VENDO_ID} | Name={shared_state.VENDO_NAME}")
        self.transition_to_running_state()

    def transition_to_running_state(self):
        self.clear_frame()
        
        self.main_frame.grid_columnconfigure(0, weight=1)
        for r in range(6):
            self.main_frame.grid_rowconfigure(r, weight=1)

        status_lbl = ctk.CTkLabel(self.main_frame, text="🚀 VENDO LIVE & ACTIVE", font=ctk.CTkFont(size=36, weight="bold"), text_color="#059669")
        status_lbl.grid(row=0, column=0, pady=(40, 5), sticky="s")

        info_lbl = ctk.CTkLabel(self.main_frame, text=f"ID: {shared_state.VENDO_ID} | Name: {shared_state.VENDO_NAME}", font=ctk.CTkFont(size=18, weight="bold"), text_color="#475569")
        info_lbl.grid(row=1, column=0, pady=5, sticky="n")

        self.system_status_label = ctk.CTkLabel(self.main_frame, text="⏳ Ready to Scan QR Code", font=ctk.CTkFont(size=24, weight="bold"), text_color="#059669", fg_color="#E6F4EA", width=750, height=140, corner_radius=16)
        self.system_status_label.grid(row=2, column=0, pady=30, sticky="nsew", padx=40)

        ctk.CTkLabel(self.main_frame, text="⌨️ Kiosk Test Input (Simulate QR Scan UID):", font=ctk.CTkFont(size=15, weight="bold"), text_color="#64748B").grid(row=3, column=0, pady=(15, 2), sticky="s")
        self.sim_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Hal. user_001 (Pindutin ang Enter)", width=500, height=45, font=ctk.CTkFont(size=14), fg_color="#FFFFFF", text_color="#0F172A", border_color="#CBD5E1")
        self.sim_entry.grid(row=4, column=0, pady=5, sticky="n")
        self.sim_entry.bind("<Return>", lambda event: self.trigger_simulated_scan())

        reset_btn = ctk.CTkButton(self.main_frame, text="⚙️ RESET KIOSK CONFIG", width=250, height=45, fg_color="#EF4444", hover_color="#DC2626", text_color="#FFFFFF", font=ctk.CTkFont(size=14, weight="bold"), corner_radius=10, command=self.confirm_hardware_reset)
        reset_btn.grid(row=5, column=0, pady=(20, 40), sticky="n")
        
        # --- LIGTAS NA FOCUS AUTOMATION ---
        def force_kiosk_focus(event=None):
            if hasattr(self, 'sim_entry') and self.sim_entry.winfo_exists():
                self.sim_entry.focus_set()
                
        self.bind("<FocusIn>", force_kiosk_focus)
        self.after(200, force_kiosk_focus)
        
        hardware.start_h2o_core_system()

    def trigger_simulated_scan(self):
        uid_input = self.sim_entry.get().strip()
        if uid_input:
            if shared_state.active_student_uid is not None:
                print(f"⚠️ KIOSK LOCK ACTIVE: Hindi pwedeng mag-simulate ng scan habang occupied ang machine.")
                self.update_status_label("⚠️ System Busy! Finish current transaction first.", "#DC2626")
                self.sim_entry.delete(0, 'end')
                return

            self.sim_entry.delete(0, 'end')
            threading.Thread(target=hardware.process_scanned_student, args=(uid_input,), daemon=True).start()

    def update_status_label(self, text, color):
        def adjust_ui():
            c_upper = str(color).upper()
            # Red/Error States
            if c_upper in ["#E74C3C", "#EF4444", "#DC2626"]:
                bg_color = "#FCE8E6"
            # Warning / Yellow / Orange States
            elif c_upper in ["#F1C40F", "#F59E0B", "#E67E22"]:
                bg_color = "#FEF3C7"
            # Blue Info States
            elif c_upper in ["#3498DB", "#3B82F6"]:
                bg_color = "#E0F2FE"
            # Green Ready/Active States
            else:
                bg_color = "#E6F4EA"

            self.system_status_label.configure(text=text, text_color=color, fg_color=bg_color)
            self.system_status_label.update_idletasks()

        self.after(0, adjust_ui)

    def confirm_hardware_reset(self):
        self.confirm_win = ctk.CTkToplevel(self)
        self.confirm_win.title("Confirmation Box")
        self.confirm_win.geometry("450x220")
        self.confirm_win.resizable(False, False)
        self.confirm_win.configure(fg_color="#FFFFFF")
        self.confirm_win.attributes("-topmost", True) 

        msg_lbl = ctk.CTkLabel(self.confirm_win, text="Do you want to reset kiosk configuration?", font=ctk.CTkFont(size=16, weight="bold"), text_color="#1E293B")
        msg_lbl.pack(pady=(45, 30))

        btn_frame = ctk.CTkFrame(self.confirm_win, fg_color="transparent")
        btn_frame.pack(fill="x", padx=40)

        yes_btn = ctk.CTkButton(btn_frame, text="Yes", fg_color="#10B981", hover_color="#059669", width=150, height=40, font=ctk.CTkFont(size=14, weight="bold"), text_color="#FFFFFF")
        yes_btn.configure(command=self.execute_hardware_wipe)
        yes_btn.pack(side="left", padx=10)

        no_btn = ctk.CTkButton(btn_frame, text="No", fg_color="#94A3B8", hover_color="#64748B", width=150, height=40, font=ctk.CTkFont(size=14, weight="bold"), text_color="#FFFFFF")
        no_btn.configure(command=self.confirm_win.destroy)
        no_btn.pack(side="right", padx=10)

    def execute_hardware_wipe(self):
        if os.path.exists(shared_state.CONFIG_FILE):
            os.remove(shared_state.CONFIG_FILE)
        if shared_state.vendo_ref:
            shared_state.vendo_ref.update({'wifi_status': 'Offline'})
        self.confirm_win.destroy()
        self.show_welcome_screen()

    def on_closing_app(self):
        print("\n🧹 Cleaning up cloud states... Setting Vendo to Offline.")
        try:
            if shared_state.vendo_ref:
                shared_state.vendo_ref.update({'wifi_status': 'Offline'})
        except Exception:
            pass

        if shared_state.esp32: 
            try:
                shared_state.esp32.write(b'STOP_PUMP\n')
            except Exception:
                pass
        self.destroy()
        os._exit(0) 

if __name__ == "__main__":
    try:
        app = H2OHubKioskSetup()
        app.mainloop()
    except KeyboardInterrupt:
        print("\n🛑 KeyboardInterrupt caught! Cleaning up...")
        try:
            if shared_state.vendo_ref:
                shared_state.vendo_ref.update({'wifi_status': 'Offline'})
        except Exception: 
            pass

        if shared_state.esp32: 
            try: 
                shared_state.esp32.write(b'STOP_PUMP\n')
            except Exception:   
                pass
        print("System Shutting Down. Bye!")
        os._exit(0)