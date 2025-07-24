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

# Placeholder for generating output file paths based on mouse ID
def generate_file_paths(mouse_id, setup_index, root_dir):
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    animal_dir = os.path.join(root_dir, mouse_id, "sleep_cam", timestamp_str)
    os.makedirs(animal_dir, exist_ok=True)
    video_path = os.path.join(animal_dir, f"setup{setup_index}.mp4")
    csv_path = os.path.join(animal_dir, f"setup{setup_index}.csv")
    return video_path, csv_path

# Simulated Arduino for fallback when real one is not found
class SimulatedArduino:
    def __init__(self):
        self.in_waiting = False

    def readline(self):
        return b""

class CameraSetup:
    def __init__(self, cam_id, com_port, root_dir):
        print(f"[DEBUG] Initializing camera {cam_id}")
        self.cam_id = cam_id
        self.com_port = com_port
        self.root_dir = root_dir
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

    def start_recording(self, mouse_id, session_duration):
        self.mouse_id = mouse_id
        self.session_duration = session_duration
        self.start_time = time.time()
        video_path, csv_path = generate_file_paths(mouse_id, self.cam_id, self.root_dir)
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

    def read_frame(self):
        ret, frame = self.cap.read()
        if ret:
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

            setup = CameraSetup(cam_id, com_port, self.root_dir)
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

    def start_recording(self):
        setup = self.setups[self.current_setup]
        mouse_id = self.mouse_id_entry.get()
        try:
            session_duration = int(self.duration_entry.get())
        except ValueError:
            session_duration = 0
        setup.start_recording(mouse_id, session_duration)

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
