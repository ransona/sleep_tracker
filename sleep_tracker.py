import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont
from tkinter import messagebox
import cv2
import threading
import time
import serial
import os
import csv
import configparser
import winreg
from datetime import datetime
from PIL import Image, ImageTk

def parse_bool(value, default=False):
    """Return a best-effort bool from config text."""
    if value is None:
        return default
    val = str(value).strip().lower()
    if val in ("1", "true", "yes", "y", "on"):
        return True
    if val in ("0", "false", "no", "n", "off"):
        return False
    return default


def generate_file_paths(mouse_id, exp_id, setup_index, root_dir):
    """Generate output file paths inside the animal/expID directory."""
    safe_mouse_id = mouse_id if mouse_id else "unknown"
    animal_dir = os.path.join(root_dir, safe_mouse_id, exp_id)
    os.makedirs(animal_dir, exist_ok=True)
    video_path = os.path.join(animal_dir, f"{exp_id}_habit.mp4")
    csv_path = os.path.join(animal_dir, f"{exp_id}_frame_times.csv")
    return video_path, csv_path


def create_exp_id(mouse_id, remote_repo):
    """
    Replicate the MATLAB newExpID logic:
    - ensure animal directory exists on the remote repository
    - find the first unused expID for today and create its directory.
    """
    safe_mouse_id = mouse_id if mouse_id else "unknown"
    os.makedirs(remote_repo, exist_ok=True)
    animal_dir = os.path.join(remote_repo, safe_mouse_id)
    os.makedirs(animal_dir, exist_ok=True)

    current_date = datetime.now().strftime("%Y-%m-%d")
    base_exp_number = 0
    while True:
        base_exp_number += 1
        base_exp_number_str = f"{base_exp_number:02d}"
        possible_exp_id = f"{current_date}_{base_exp_number_str}_{safe_mouse_id}"
        candidate_dir = os.path.join(animal_dir, possible_exp_id)
        if not os.path.exists(candidate_dir):
            os.makedirs(candidate_dir, exist_ok=True)
            return possible_exp_id, candidate_dir


def append_exp_list(exp_list_dir, exp_id):
    """Append expID and timestamp to the shared exp_list.txt file."""
    exp_list_path = os.path.join(exp_list_dir, "exp_list.txt")
    os.makedirs(exp_list_dir, exist_ok=True)
    with open(exp_list_path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([exp_id, datetime.now().isoformat()])

# Simulated Arduino for fallback when real one is not found
class SimulatedArduino:
    def __init__(self):
        self.in_waiting = False

    def readline(self):
        return b""

    def write(self, _data):
        return 0

class CameraSetup:
    def __init__(
        self,
        cam_id,
        com_port,
        root_dir,
        flip_horizontal=False,
        flip_vertical=False,
        logger=None,
        name=None,
        capture_factory=None
    ):
        self.logger = logger
        self._log("Initializing camera", level="DEBUG", suffix=f" {cam_id}")
        self.name = name
        self.cam_id = cam_id
        self.com_port = com_port
        self.root_dir = root_dir
        self.flip_horizontal = flip_horizontal
        self.flip_vertical = flip_vertical
        if capture_factory is None:
            capture_factory = cv2.VideoCapture
        self.cap = capture_factory(cam_id)
        try:
            if com_port is not None:
                self.serial = serial.Serial(com_port, 9600, timeout=0.1, write_timeout=1.0)
                self._log(f"Arduino connected on {com_port}", level="DEBUG")
            else:
                raise serial.SerialException()
        except (serial.SerialException, FileNotFoundError):
            self._log(f"Arduino not found on {com_port}. Using simulated Arduino.", level="WARNING")
            self.serial = SimulatedArduino()
        self.recording = False
        self.writer = None
        self.csv_file = None
        self.csv_writer = None
        self.start_time = None
        self.elapsed_time = 0
        self.mouse_id = ""
        self.session_duration = 0  # in minutes
        self.exp_id = ""
        self.lock_state = "automatic"
        self.latest_status = None
        self.last_arduino_line = ""
        self.last_logged_arduino_line = ""
        self.serial_lock = threading.Lock()

    def _log(self, message, level="INFO", suffix=""):
        if self.logger:
            self.logger(message + suffix, level=level)
        else:
            if level == "DEBUG":
                print(f"{message}{suffix}")
            else:
                print(f"[{level}] {message}{suffix}")

    def start_recording(self, mouse_id, session_duration, exp_id):
        self.mouse_id = mouse_id
        self.session_duration = session_duration
        self.start_time = time.time()
        video_path, csv_path = generate_file_paths(mouse_id, exp_id, self.cam_id, self.root_dir)
        meta_path = os.path.join(os.path.dirname(video_path), f"{exp_id}_meta.txt")
        with open(meta_path, "w", newline="") as meta_file:
            meta_file.write(self.name or "")
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.writer = cv2.VideoWriter(video_path, fourcc, 20.0, (width, height))
        self.csv_file = open(csv_path, 'w', newline='')
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(['timestamp', 'arduino_data'])
        self.recording = True

    def stop_recording(self):
        self.recording = False
        if self.writer:
            self.writer.release()
        if self.csv_file:
            self.csv_file.close()

    def apply_flips(self, frame):
        if self.flip_horizontal:
            frame = cv2.flip(frame, 1)
        if self.flip_vertical:
            frame = cv2.flip(frame, 0)
        return frame

    def read_frame(self):
        ret, frame = self.cap.read()
        arduino_data = ""
        with self.serial_lock:
            while self.serial.in_waiting:
                arduino_data = self.serial.readline().decode().strip()
            if arduino_data:
                self.latest_status = self.parse_arduino_status(arduino_data)
                self.last_arduino_line = arduino_data
        if ret:
            frame = self.apply_flips(frame)
            if self.recording:
                timestamp = time.time() - self.start_time
                self.writer.write(frame)
                self.csv_writer.writerow([timestamp, arduino_data])
                self.elapsed_time = int(timestamp)
            return frame
        return None

    def parse_arduino_status(self, line):
        parts = [part.strip() for part in line.split(";")]
        if len(parts) != 3:
            return None
        brake_raw, wheel_pos, mode = parts
        brake_text = "locked" if brake_raw == "0" else "unlocked" if brake_raw == "1" else brake_raw
        return (brake_text, wheel_pos, mode)

    def send_lock_state(self, log_fn=None):
        command_map = {
            "automatic": b"a",
            "locked": b"b",
            "unlocked": b"c",
        }
        command = command_map.get(self.lock_state)
        if command is None:
            return
        def _send():
            attempts = 0
            while attempts < 3:
                attempts += 1
                try:
                    with self.serial_lock:
                        self.serial.write(command)
                    if log_fn:
                        log_fn(f"Sent lock state '{self.lock_state}' ({command.decode()})")
                    return
                except Exception as exc:
                    self._log(self.format_serial_error(exc), level="WARNING")
                    time.sleep(0.1)
            self._log("Giving up after 3 send attempts.", level="WARNING")

        threading.Thread(target=_send, daemon=True).start()

    def format_serial_error(self, exc):
        is_open = getattr(self.serial, "is_open", None)
        in_waiting = getattr(self.serial, "in_waiting", None)
        out_waiting = getattr(self.serial, "out_waiting", None)
        return f"Serial write failed: {exc} (is_open={is_open}, in_waiting={in_waiting}, out_waiting={out_waiting})"

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Multi-Camera Acquisition")
        self.root.state("zoomed")
        self.setups = []
        self.current_setup = 0
        self.running = True
        self.last_elapsed = 0
        self.auto_cycle = False
        self.auto_cycle_interval = 5

        self.status_dialog = None
        self.status_text = None
        self.create_status_dialog()
        self.log("Starting application", level="DEBUG")
        self.load_config()
        self.log("Configuration loaded", level="DEBUG")
        self.build_gui()
        self.log("GUI built", level="DEBUG")
        self.close_status_dialog()
        self.update_video()
        self.auto_cycle_loop()

    def load_config(self):
        self.log("Reading configuration file...", level="DEBUG")
        config = configparser.ConfigParser()
        config.read('configuration.txt')
        self.log("Configuration file loaded.", level="DEBUG")
        self.root_dir = config['DEFAULT']['RootDirectory']
        self.remote_repo = config['DEFAULT'].get('RemoteRepository', r'\\ar-lab-nas1\\DataServer\\Remote_Repository')
        self.exp_list_dir = config['DEFAULT'].get('ExperimentListDirectory', r'\\ar-lab-nas1\\DataServer\\Remote_Repository\\habituation')
        self.log(f"Checking root directory: {self.root_dir}", level="DEBUG")
        if not os.path.exists(self.root_dir):
            self.log(f"Root directory '{self.root_dir}' not found. Creating it.", level="INFO")
            os.makedirs(self.root_dir)

        section_names = config.sections()
        for section_name in section_names:
            if section_name.upper() == "DEFAULT":
                continue
            if "CameraID" not in config[section_name]:
                continue
            cam_id = self.parse_camera_id(config[section_name]["CameraID"])
            com_port = config[section_name]["COMPort"]
            self.log(f"{section_name}: Checking camera {cam_id} and COM port {com_port}", level="DEBUG")

            cap = self.open_capture(cam_id)
            if not cap.isOpened():
                self.log(f"{section_name}: Camera ID {cam_id} could not be opened. Skipping this setup.", level="ERROR")
                cap.release()
                continue
            cap.release()

            flip_horizontal = parse_bool(config[section_name].get('FlipHorizontal', False))
            flip_vertical = parse_bool(config[section_name].get('FlipVertical', False))

            setup_name = section_name
            setup = CameraSetup(
                cam_id,
                com_port,
                self.root_dir,
                flip_horizontal=flip_horizontal,
                flip_vertical=flip_vertical,
                logger=self.log,
                name=setup_name,
                capture_factory=self.open_capture
            )
            self.setups.append(setup)
            self.log(f"{section_name} initialized.", level="DEBUG")

        if not self.setups:
            self.log("No valid camera setups found. Exiting.", level="ERROR")
            self.root.quit()

    def create_status_dialog(self):
        self.status_dialog = tk.Toplevel(self.root)
        self.status_dialog.title("Initializing")
        screen_w = max(1, self.root.winfo_screenwidth())
        screen_h = max(1, self.root.winfo_screenheight())
        dialog_w = max(300, int(screen_w * 0.2))
        dialog_h = max(200, int(screen_h * 0.2))
        x = max(0, (screen_w - dialog_w) // 2)
        y = max(0, (screen_h - dialog_h) // 2)
        self.status_dialog.geometry(f"{dialog_w}x{dialog_h}+{x}+{y}")
        self.status_dialog.transient(self.root)
        label = ttk.Label(self.status_dialog, text="Initializing, please wait...")
        label.pack(padx=10, pady=(10, 0))
        list_frame = ttk.Frame(self.status_dialog)
        list_frame.pack(padx=10, pady=10, fill="both", expand=True)
        self.status_text = tk.Listbox(list_frame)
        self.status_text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.status_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.status_text.config(yscrollcommand=scrollbar.set)
        self.status_dialog.update()

    def close_status_dialog(self):
        if self.status_dialog is not None:
            self.status_dialog.destroy()
            self.status_dialog = None
            self.status_text = None

    def log(self, message, level="INFO"):
        if level == "DEBUG":
            line = message
        else:
            line = f"[{level}] {message}"
        print(line)
        if self.status_text is not None and self.status_dialog is not None:
            self.status_text.insert("end", line)
            self.status_text.see("end")
            self.status_dialog.update()

    def parse_camera_id(self, value):
        text = str(value).strip()
        try:
            return int(text)
        except ValueError:
            return text

    def open_capture(self, cam_id):
        if isinstance(cam_id, int):
            return cv2.VideoCapture(cam_id)
        return cv2.VideoCapture(cam_id, cv2.CAP_DSHOW)

    def build_gui(self):
        default_font = tkfont.nametofont("TkDefaultFont")
        self.root.update_idletasks()
        screen_height = max(1, self.root.winfo_screenheight())
        base_size = max(1, abs(default_font.actual()["size"]))
        scaled_size = max(base_size, int(screen_height * 0.035))
        large_font = tkfont.Font(
            family=default_font.actual()["family"],
            size=scaled_size
        )
        self.large_font = large_font
        self.small_font = tkfont.Font(
            family=default_font.actual()["family"],
            size=base_size
        )

        self.mouse_id_label = ttk.Label(self.root, text="Mouse ID:", font=large_font)
        self.mouse_id_label.pack()
        self.mouse_id_entry = ttk.Entry(self.root, font=large_font, justify="center")
        self.mouse_id_entry.pack()

        self.duration_label = ttk.Label(self.root, text="Session Duration (min):")
        self.duration_label.pack()
        self.duration_entry = ttk.Entry(self.root, justify="center")
        self.duration_entry.pack()

        self.video_panel = ttk.Label(self.root)
        self.video_panel.pack()

        self.setup_label = ttk.Label(self.root, text="", font=large_font)
        self.setup_label.pack()

        self.arduino_status_label = ttk.Label(self.root, text="", font=self.small_font)
        self.arduino_status_label.pack()

        self.timer_label = ttk.Label(
            self.root,
            text="Elapsed: 0:00 | Remaining: 0:00",
            font=large_font
        )
        self.timer_label.pack()

        control_frame = ttk.Frame(self.root)
        control_frame.pack(fill="x")

        button_frame = ttk.Frame(control_frame)
        button_frame.pack()

        self.start_button = ttk.Button(button_frame, text="Start", command=self.start_recording)
        self.start_button.grid(row=0, column=0)
        self.stop_button = ttk.Button(button_frame, text="Stop", command=self.stop_recording)
        self.stop_button.grid(row=0, column=1)
        self.left_button = ttk.Button(button_frame, text="<", command=self.prev_setup)
        self.left_button.grid(row=0, column=2)
        self.right_button = ttk.Button(button_frame, text=">", command=self.next_setup)
        self.right_button.grid(row=0, column=3)

        self.auto_cycle_var = tk.BooleanVar()
        self.auto_cycle_button = ttk.Checkbutton(button_frame, text="Auto Cycle", variable=self.auto_cycle_var, command=self.toggle_auto_cycle)
        self.auto_cycle_button.grid(row=0, column=4)

        self.dwell_label = ttk.Label(button_frame, text="Dwell (s):")
        self.dwell_label.grid(row=0, column=5)
        self.dwell_entry = ttk.Entry(button_frame, width=5)
        self.dwell_entry.insert(0, "5")
        self.dwell_entry.grid(row=0, column=6)

        self.lock_state_button = tk.Button(button_frame, text="Automatic", command=self.toggle_lock_state, bg="blue", fg="white")
        self.lock_state_button.grid(row=1, column=0, columnspan=7, pady=(10, 0))

        self.debug_var = tk.BooleanVar()
        self.debug_checkbox = ttk.Checkbutton(button_frame, text="Debug", variable=self.debug_var)
        self.debug_checkbox.grid(row=1, column=7, padx=(10, 0), pady=(10, 0))
        self.update_setup_label()
        self.update_lock_state_button()

    def update_video(self):
        setup = self.setups[self.current_setup]
        frame = setup.read_frame()
        if frame is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            imgtk = ImageTk.PhotoImage(image=img)
            self.video_panel.imgtk = imgtk
            self.video_panel.config(image=imgtk)
        if setup.recording and setup.last_arduino_line and setup.last_arduino_line != setup.last_logged_arduino_line:
            self.debug_log(f"{setup.name}: {setup.last_arduino_line}")
            setup.last_logged_arduino_line = setup.last_arduino_line

        elapsed = setup.elapsed_time if setup.recording else 0
        remaining = max(0, (setup.session_duration * 60) - elapsed)
        elapsed_str = f"{elapsed // 60}:{elapsed % 60:02d}"
        remaining_str = f"{remaining // 60}:{remaining % 60:02d}"

        if not setup.recording:
            color = "black"
        elif remaining == 0 and setup.session_duration > 0:
            color = "red"
        elif remaining < 5 * 60 and setup.session_duration > 0:
            color = "orange"
        else:
            color = "green"

        self.timer_label.config(
            text=f"Elapsed: {elapsed_str} | Remaining: {remaining_str}",
            foreground=color
        )
        self.update_setup_label()

        if self.running:
            self.root.after(30, self.update_video)

    def auto_cycle_loop(self):
        if self.auto_cycle:
            self.next_setup()
        if self.running:
            try:
                interval = int(self.dwell_entry.get())
            except ValueError:
                interval = 5
            self.root.after(interval * 1000, self.auto_cycle_loop)

    def toggle_auto_cycle(self):
        self.auto_cycle = self.auto_cycle_var.get()

    def generate_and_register_exp(self, mouse_id):
        try:
            exp_id, remote_exp_dir = create_exp_id(mouse_id, self.remote_repo)
        except Exception as exc:
            self.log(f"Failed to create remote expID directory: {exc}", level="WARNING")
            exp_id = f"{datetime.now().strftime('%Y-%m-%d')}_01_{mouse_id or 'unknown'}"
            remote_exp_dir = None
        else:
            try:
                append_exp_list(self.exp_list_dir, exp_id)
            except Exception as exc:
                self.log(f"Failed to append experiment list for '{exp_id}': {exc}", level="WARNING")
        local_exp_dir = os.path.join(self.root_dir, mouse_id or "unknown", exp_id)
        os.makedirs(local_exp_dir, exist_ok=True)
        self.log(f"Using expID '{exp_id}'. Local path: {local_exp_dir}", level="INFO")
        if remote_exp_dir:
            self.log(f"Remote experiment directory: {remote_exp_dir}", level="INFO")
        return exp_id, remote_exp_dir

    def start_recording(self):
        setup = self.setups[self.current_setup]
        mouse_id = self.mouse_id_entry.get().strip()
        if not mouse_id:
            messagebox.showerror("Missing Mouse ID", "Please enter a Mouse ID before starting.")
            return
        try:
            session_duration = int(self.duration_entry.get())
        except ValueError:
            session_duration = 0

        if session_duration == 0:
            proceed = messagebox.askyesno(
                "Session Duration",
                "Session duration is set to 0 minutes. Continue anyway?"
            )
            if not proceed:
                return

        exp_id, remote_exp_dir = self.generate_and_register_exp(mouse_id)
        setup.exp_id = exp_id
        setup.start_recording(mouse_id, session_duration, exp_id)
        setup.send_lock_state(log_fn=self.debug_log)
        self.update_setup_label()

    def stop_recording(self):
        self.setups[self.current_setup].stop_recording()

    def prev_setup(self):
        self.save_current_setup_settings()
        self.current_setup = (self.current_setup - 1) % len(self.setups)
        self.load_current_setup_settings()

    def next_setup(self):
        self.save_current_setup_settings()
        self.current_setup = (self.current_setup + 1) % len(self.setups)
        self.load_current_setup_settings()

    def update_setup_label(self):
        setup = self.setups[self.current_setup]
        exp_suffix = f": {setup.exp_id}" if setup.exp_id else ""
        label_name = setup.name or f"Setup{self.current_setup}"
        status_line = ""
        if setup.latest_status:
            lock_text, wheel_pos, mode = setup.latest_status
            status_line = f"{lock_text}, {wheel_pos}, mode: {mode}"
        self.setup_label.config(text=f"{label_name}{exp_suffix}")
        self.arduino_status_label.config(text=status_line)

    def debug_log(self, message):
        if self.debug_var.get():
            print(message)

    def update_lock_state_button(self):
        setup = self.setups[self.current_setup]
        color_map = {
            "automatic": ("Automatic", "blue", "white"),
            "unlocked": ("Unlocked", "green", "white"),
            "locked": ("Locked", "red", "white"),
        }
        label, bg, fg = color_map.get(setup.lock_state, ("Automatic", "blue", "white"))
        self.lock_state_button.config(text=label, bg=bg, fg=fg)

    def toggle_lock_state(self):
        setup = self.setups[self.current_setup]
        if setup.lock_state == "automatic":
            setup.lock_state = "unlocked"
        elif setup.lock_state == "unlocked":
            setup.lock_state = "locked"
        else:
            setup.lock_state = "automatic"
        setup.send_lock_state(log_fn=self.debug_log)
        self.update_lock_state_button()

    def get_directshow_device_paths(self):
        device_class_guid = "{e5323777-f976-4f5b-9b55-b94699c46e44}"
        paths = []
        try:
            base_key = rf"SYSTEM\\CurrentControlSet\\Control\\DeviceClasses\\{device_class_guid}"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_key) as key:
                index = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    paths.append(subkey_name)
                    index += 1
        except OSError as exc:
            self.log(f"Failed to read DirectShow device paths: {exc}", level="WARNING")
        return paths

    def enumerate_camera_entries(self):
        paths = self.get_directshow_device_paths()
        max_index = min(16, max(4, len(paths)))
        entries = []
        for index in range(max_index):
            cap = cv2.VideoCapture(index)
            if cap.isOpened():
                cap.release()
                path = paths[index] if index < len(paths) else ""
                entries.append({"id": index, "path": path})
            else:
                cap.release()
        return entries

    def show_camera_streams(self):
        if hasattr(self, "camera_viewer") and self.camera_viewer is not None:
            if self.camera_viewer.winfo_exists():
                self.camera_viewer.lift()
                return

        entries = self.enumerate_camera_entries()
        self.camera_viewer = tk.Toplevel(self.root)
        self.camera_viewer.title("Available Cameras")
        self.camera_viewer.geometry("1000x700")
        self.camera_viewer.transient(self.root)

        if not entries:
            ttk.Label(self.camera_viewer, text="No cameras found.").pack(padx=20, pady=20)
            return

        self.camera_viewer_items = []
        columns = 2
        for idx, entry in enumerate(entries):
            frame = ttk.Frame(self.camera_viewer)
            row = idx // columns
            col = idx % columns
            frame.grid(row=row, column=col, padx=10, pady=10, sticky="nsew")
            self.camera_viewer.grid_columnconfigure(col, weight=1)
            self.camera_viewer.grid_rowconfigure(row, weight=1)

            label_text = f"Device {entry['id']}"
            if entry["path"]:
                label_text += f"\n{entry['path']}"
            ttk.Label(frame, text=label_text, justify="center", wraplength=480).pack()
            panel = ttk.Label(frame)
            panel.pack(fill="both", expand=True)

            cap = self.open_capture(entry["id"])
            if not cap.isOpened():
                panel.config(text="Unable to open camera stream.")
                cap.release()
                continue

            self.camera_viewer_items.append({"cap": cap, "panel": panel})

        self.camera_viewer.protocol("WM_DELETE_WINDOW", self.close_camera_viewer)
        self.update_camera_viewer()

    def update_camera_viewer(self):
        if not hasattr(self, "camera_viewer") or self.camera_viewer is None:
            return
        if not self.camera_viewer.winfo_exists():
            return
        for item in self.camera_viewer_items:
            cap = item["cap"]
            panel = item["panel"]
            ret, frame = cap.read()
            if ret:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                imgtk = ImageTk.PhotoImage(image=img)
                panel.imgtk = imgtk
                panel.config(image=imgtk)
        self.camera_viewer.after(100, self.update_camera_viewer)

    def close_camera_viewer(self):
        if hasattr(self, "camera_viewer_items"):
            for item in self.camera_viewer_items:
                item["cap"].release()
            self.camera_viewer_items = []
        if hasattr(self, "camera_viewer") and self.camera_viewer is not None:
            self.camera_viewer.destroy()
            self.camera_viewer = None

    def save_current_setup_settings(self):
        setup = self.setups[self.current_setup]
        setup.mouse_id = self.mouse_id_entry.get()
        try:
            setup.session_duration = int(self.duration_entry.get())
        except ValueError:
            setup.session_duration = 0

    def load_current_setup_settings(self):
        setup = self.setups[self.current_setup]
        self.mouse_id_entry.delete(0, tk.END)
        self.mouse_id_entry.insert(0, setup.mouse_id)
        self.duration_entry.delete(0, tk.END)
        self.duration_entry.insert(0, str(setup.session_duration))
        self.update_setup_label()
        self.update_lock_state_button()

    def on_closing(self):
        self.running = False
        for setup in self.setups:
            setup.cap.release()
            setup.stop_recording()
        self.root.destroy()

if __name__ == '__main__':
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()
