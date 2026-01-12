import tkinter as tk
from tkinter import ttk
import cv2
import threading
import time
import serial
import os
import csv
import configparser
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
    video_path = os.path.join(animal_dir, f"setup{setup_index}.mp4")
    csv_path = os.path.join(animal_dir, f"setup{setup_index}.csv")
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
        if base_exp_number < 10:
            base_exp_number_str = f"0{base_exp_number}"
        else:
            base_exp_number_str = str(base_exp_number)
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

class CameraSetup:
    def __init__(self, cam_id, com_port, root_dir, flip_horizontal=False, flip_vertical=False):
        print(f"[DEBUG] Initializing camera {cam_id}")
        self.cam_id = cam_id
        self.com_port = com_port
        self.root_dir = root_dir
        self.flip_horizontal = flip_horizontal
        self.flip_vertical = flip_vertical
        self.cap = cv2.VideoCapture(cam_id)
        try:
            if com_port is not None:
                self.serial = serial.Serial(com_port, 9600, timeout=0.1)
                print(f"[DEBUG] Arduino connected on {com_port}")
            else:
                raise serial.SerialException()
        except (serial.SerialException, FileNotFoundError):
            print(f"[WARNING] Arduino not found on {com_port}. Using simulated Arduino.")
            self.serial = SimulatedArduino()
        self.recording = False
        self.writer = None
        self.csv_file = None
        self.csv_writer = None
        self.start_time = None
        self.elapsed_time = 0
        self.mouse_id = ""
        self.session_duration = 0  # in minutes

    def start_recording(self, mouse_id, session_duration, exp_id):
        self.mouse_id = mouse_id
        self.session_duration = session_duration
        self.start_time = time.time()
        video_path, csv_path = generate_file_paths(mouse_id, exp_id, self.cam_id, self.root_dir)
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
        if ret:
            frame = self.apply_flips(frame)
            if self.recording:
                timestamp = time.time() - self.start_time
                self.writer.write(frame)
                arduino_data = ""
                if self.serial.in_waiting:
                    arduino_data = self.serial.readline().decode().strip()
                self.csv_writer.writerow([timestamp, arduino_data])
                self.elapsed_time = int(timestamp)
            return frame
        return None

class App:
    def __init__(self, root):
        print("[DEBUG] Starting application")
        self.root = root
        self.root.title("Multi-Camera Acquisition")
        self.setups = []
        self.current_setup = 0
        self.running = True
        self.last_elapsed = 0
        self.auto_cycle = False
        self.auto_cycle_interval = 5

        self.load_config()
        print("[DEBUG] Configuration loaded")
        self.build_gui()
        print("[DEBUG] GUI built")
        self.update_video()
        self.auto_cycle_loop()

    def load_config(self):
        print("[DEBUG] Reading configuration file...")
        config = configparser.ConfigParser()
        config.read('configuration.txt')
        print("[DEBUG] Configuration file loaded.")
        self.root_dir = config['DEFAULT']['RootDirectory']
        self.remote_repo = config['DEFAULT'].get('RemoteRepository', r'\\ar-lab-nas1\\DataServer\\Remote_Repository')
        self.exp_list_dir = config['DEFAULT'].get('ExperimentListDirectory', r'\\ar-lab-nas1\\DataServer\\Remote_Repository\\habituation')
        print(f"[DEBUG] Checking root directory: {self.root_dir}")
        if not os.path.exists(self.root_dir):
            print(f"[INFO] Root directory '{self.root_dir}' not found. Creating it.")
            os.makedirs(self.root_dir)

        index = 0
        while f'Setup{index}' in config:
            cam_id = int(config[f'Setup{index}']['CameraID'])
            com_port = config[f'Setup{index}']['COMPort']
            print(f"[DEBUG] Setup{index}: Checking camera {cam_id} and COM port {com_port}")

            cap = cv2.VideoCapture(cam_id)
            if not cap.isOpened():
                print(f"[ERROR] Setup{index}: Camera ID {cam_id} could not be opened. Skipping this setup.")
                cap.release()
                index += 1
                continue
            cap.release()

            flip_horizontal = parse_bool(config[f'Setup{index}'].get('FlipHorizontal', False))
            flip_vertical = parse_bool(config[f'Setup{index}'].get('FlipVertical', False))

            setup = CameraSetup(cam_id, com_port, self.root_dir, flip_horizontal=flip_horizontal, flip_vertical=flip_vertical)
            self.setups.append(setup)
            print(f"[DEBUG] Setup{index} initialized.")
            index += 1

        if not self.setups:
            print("[ERROR] No valid camera setups found. Exiting.")
            self.root.quit()

    def build_gui(self):
        self.mouse_id_label = ttk.Label(self.root, text="Mouse ID:")
        self.mouse_id_label.pack()
        self.mouse_id_entry = ttk.Entry(self.root)
        self.mouse_id_entry.pack()

        self.duration_label = ttk.Label(self.root, text="Session Duration (min):")
        self.duration_label.pack()
        self.duration_entry = ttk.Entry(self.root)
        self.duration_entry.pack()

        self.video_panel = ttk.Label(self.root)
        self.video_panel.pack()

        self.timer_label = ttk.Label(self.root, text="Elapsed: 0:00 | Remaining: 0:00")
        self.timer_label.pack()

        control_frame = ttk.Frame(self.root)
        control_frame.pack()

        self.start_button = ttk.Button(control_frame, text="Start", command=self.start_recording)
        self.start_button.grid(row=0, column=0)
        self.stop_button = ttk.Button(control_frame, text="Stop", command=self.stop_recording)
        self.stop_button.grid(row=0, column=1)
        self.left_button = ttk.Button(control_frame, text="<", command=self.prev_setup)
        self.left_button.grid(row=0, column=2)
        self.right_button = ttk.Button(control_frame, text=">", command=self.next_setup)
        self.right_button.grid(row=0, column=3)

        self.auto_cycle_var = tk.BooleanVar()
        self.auto_cycle_button = ttk.Checkbutton(control_frame, text="Auto Cycle", variable=self.auto_cycle_var, command=self.toggle_auto_cycle)
        self.auto_cycle_button.grid(row=0, column=4)

        self.dwell_label = ttk.Label(control_frame, text="Dwell (s):")
        self.dwell_label.grid(row=0, column=5)
        self.dwell_entry = ttk.Entry(control_frame, width=5)
        self.dwell_entry.insert(0, "5")
        self.dwell_entry.grid(row=0, column=6)

    def update_video(self):
        setup = self.setups[self.current_setup]
        frame = setup.read_frame()
        if frame is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            imgtk = ImageTk.PhotoImage(image=img)
            self.video_panel.imgtk = imgtk
            self.video_panel.config(image=imgtk)

        elapsed = setup.elapsed_time if setup.recording else 0
        remaining = max(0, (setup.session_duration * 60) - elapsed)
        elapsed_str = f"{elapsed // 60}:{elapsed % 60:02d}"
        remaining_str = f"{remaining // 60}:{remaining % 60:02d}"

        if setup.session_duration > 0 and elapsed > setup.session_duration * 60:
            self.timer_label.config(text=f"Elapsed: {elapsed_str} | Remaining: {remaining_str}", foreground="red")
        else:
            self.timer_label.config(text=f"Elapsed: {elapsed_str} | Remaining: {remaining_str}", foreground="black")

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
            print(f"[WARNING] Failed to create remote expID directory: {exc}")
            fallback_suffix = f"{int(time.time())}"
            exp_id = f"{datetime.now().strftime('%Y-%m-%d')}_local_{fallback_suffix}_{mouse_id or 'unknown'}"
            remote_exp_dir = None
        else:
            try:
                append_exp_list(self.exp_list_dir, exp_id)
            except Exception as exc:
                print(f"[WARNING] Failed to append experiment list for '{exp_id}': {exc}")
        local_exp_dir = os.path.join(self.root_dir, mouse_id or "unknown", exp_id)
        os.makedirs(local_exp_dir, exist_ok=True)
        print(f"[INFO] Using expID '{exp_id}'. Local path: {local_exp_dir}")
        if remote_exp_dir:
            print(f"[INFO] Remote experiment directory: {remote_exp_dir}")
        return exp_id, remote_exp_dir

    def start_recording(self):
        setup = self.setups[self.current_setup]
        mouse_id = self.mouse_id_entry.get()
        try:
            session_duration = int(self.duration_entry.get())
        except ValueError:
            session_duration = 0

        exp_id, remote_exp_dir = self.generate_and_register_exp(mouse_id)
        setup.start_recording(mouse_id, session_duration, exp_id)

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
