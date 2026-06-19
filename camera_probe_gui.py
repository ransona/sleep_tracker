import configparser
import tkinter as tk
from tkinter import simpledialog, ttk

import cv2
from PIL import Image, ImageTk


CAPTURE_BACKEND_NAME = "DSHOW"
CAPTURE_BACKEND = cv2.CAP_DSHOW
DEFAULT_CONFIG_PATH = "configuration.txt"
FRAME_INTERVAL_MS = 50


def parse_camera_id(value):
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        return text


def load_camera_entries(config_path):
    config = configparser.ConfigParser()
    config.read(config_path)
    entries = []
    for section_name in config.sections():
        section = config[section_name]
        if "CameraID" not in section:
            continue
        entries.append(
            {
                "name": section_name,
                "camera_id": parse_camera_id(section["CameraID"]),
            }
        )
    return entries


class CameraProbeApp:
    def __init__(self, root, config_path=DEFAULT_CONFIG_PATH):
        self.root = root
        self.root.title("Camera Probe")
        self.root.state("zoomed")

        self.config_path = config_path
        self.entries = load_camera_entries(config_path)
        if not self.entries:
            self.entries = [{"name": "Camera 0", "camera_id": 0}]

        self.cap = None
        self.backend_name = "N/A"
        self.current_index = 0
        self.last_frame = None

        self.exposure_step_var = tk.StringVar(value="1")
        self.gain_step_var = tk.StringVar(value="1")
        self.status_var = tk.StringVar(value="Opening camera...")
        self.exposure_var = tk.StringVar(value="Exposure: --")
        self.gain_var = tk.StringVar(value="Gain: --")
        self.auto_exposure_var = tk.StringVar(value="Auto Exposure: --")
        self.auto_gain_var = tk.StringVar(
            value="Auto Gain: not exposed by OpenCV; manual gain writes only"
        )

        self.build_ui()
        self.open_selected_camera()
        self.update_video()

    def build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=10)

        ttk.Label(top, text="Camera:").grid(row=0, column=0, sticky="w")
        option_names = [f"{entry['name']} (ID {entry['camera_id']})" for entry in self.entries]
        self.camera_var = tk.StringVar(value=option_names[0])
        self.camera_menu = ttk.OptionMenu(top, self.camera_var, option_names[0], *option_names, command=self.on_camera_change)
        self.camera_menu.grid(row=0, column=1, sticky="w", padx=(5, 10))

        ttk.Button(top, text="Reopen", command=self.open_selected_camera).grid(row=0, column=2, padx=(0, 10))
        ttk.Label(top, textvariable=self.status_var).grid(row=0, column=3, sticky="w")

        self.video_panel = ttk.Label(self.root)
        self.video_panel.pack(padx=10, pady=(0, 10))

        info = ttk.Frame(self.root)
        info.pack(fill="x", padx=10)
        ttk.Label(info, textvariable=self.auto_exposure_var).grid(row=0, column=0, sticky="w", padx=(0, 15))
        ttk.Label(info, textvariable=self.exposure_var).grid(row=0, column=1, sticky="w", padx=(0, 15))
        self.gain_value_label = ttk.Label(info, textvariable=self.gain_var, cursor="hand2")
        self.gain_value_label.grid(row=0, column=2, sticky="w", padx=(0, 15))
        self.gain_value_label.bind("<Double-Button-1>", self.prompt_set_gain)
        ttk.Label(info, textvariable=self.auto_gain_var).grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 0))

        controls = ttk.Frame(self.root)
        controls.pack(fill="x", padx=10, pady=10)

        ttk.Label(controls, text="Exposure Step").grid(row=0, column=0, sticky="e")
        ttk.Entry(controls, width=6, textvariable=self.exposure_step_var).grid(row=0, column=1, padx=(5, 10))
        ttk.Button(controls, text="Exposure -", command=lambda: self.step_property("exposure", -1)).grid(row=0, column=2, padx=5)
        ttk.Button(controls, text="Exposure +", command=lambda: self.step_property("exposure", 1)).grid(row=0, column=3, padx=5)

        ttk.Label(controls, text="Gain Step").grid(row=1, column=0, sticky="e", pady=(10, 0))
        ttk.Entry(controls, width=6, textvariable=self.gain_step_var).grid(row=1, column=1, padx=(5, 10), pady=(10, 0))
        ttk.Button(controls, text="Gain -", command=lambda: self.step_property("gain", -1)).grid(row=1, column=2, padx=5, pady=(10, 0))
        ttk.Button(controls, text="Gain +", command=lambda: self.step_property("gain", 1)).grid(row=1, column=3, padx=5, pady=(10, 0))

        ttk.Button(controls, text="Refresh Values", command=self.refresh_property_labels).grid(row=2, column=0, columnspan=2, sticky="w", pady=(15, 0))
        ttk.Button(controls, text="Force Manual Exposure", command=self.force_manual_controls).grid(row=2, column=2, columnspan=2, sticky="w", pady=(15, 0))

    def on_camera_change(self, selected_label):
        for index, entry in enumerate(self.entries):
            label = f"{entry['name']} (ID {entry['camera_id']})"
            if label == selected_label:
                self.current_index = index
                break
        self.open_selected_camera()

    def open_selected_camera(self):
        self.release_camera()
        entry = self.entries[self.current_index]
        camera_id = entry["camera_id"]

        cap = cv2.VideoCapture(camera_id, CAPTURE_BACKEND)
        backend_name = CAPTURE_BACKEND_NAME
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(camera_id)
            backend_name = "DEFAULT"

        self.cap = cap
        self.backend_name = backend_name
        if not self.cap.isOpened():
            self.status_var.set(f"{entry['name']}: failed to open camera {camera_id}")
            self.refresh_property_labels()
            return

        self.force_manual_controls()
        self.status_var.set(f"{entry['name']}: opened camera {camera_id} via {backend_name}")
        self.refresh_property_labels()

    def release_camera(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.last_frame = None

    def force_manual_controls(self):
        if self.cap is None or not self.cap.isOpened():
            return
        # Common manual-exposure values across OpenCV backends/drivers.
        for value in (0.25, 0.0, 1.0, -1.0):
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, value)
        self.refresh_property_labels()

    def get_step_size(self, property_name):
        raw = self.exposure_step_var.get() if property_name == "exposure" else self.gain_step_var.get()
        try:
            step = float(raw)
        except ValueError:
            step = 1.0
        return step if step > 0 else 1.0

    def step_property(self, property_name, direction):
        if self.cap is None or not self.cap.isOpened():
            return

        self.force_manual_controls()

        if property_name == "exposure":
            prop_id = cv2.CAP_PROP_EXPOSURE
        else:
            prop_id = cv2.CAP_PROP_GAIN

        before = self.cap.get(prop_id)
        step = self.get_step_size(property_name)
        target = before + (direction * step)
        ok = self.cap.set(prop_id, target)
        after = self.cap.get(prop_id)
        self.status_var.set(
            f"{property_name.title()} target={target:.4f}, ok={ok}, reported={after:.4f}"
        )
        self.refresh_property_labels()

    def prompt_set_gain(self, _event=None):
        if self.cap is None or not self.cap.isOpened():
            return

        current_gain = self.cap.get(cv2.CAP_PROP_GAIN)
        requested = simpledialog.askfloat(
            "Set Gain",
            "Enter new gain value:",
            initialvalue=current_gain,
            parent=self.root,
        )
        if requested is None:
            return

        ok = self.cap.set(cv2.CAP_PROP_GAIN, requested)
        reported = self.cap.get(cv2.CAP_PROP_GAIN)
        self.status_var.set(
            f"Gain target={requested:.4f}, ok={ok}, reported={reported:.4f}"
        )
        self.refresh_property_labels()

    def refresh_property_labels(self):
        if self.cap is None or not self.cap.isOpened():
            self.auto_exposure_var.set("Auto Exposure: --")
            self.exposure_var.set("Exposure: --")
            self.gain_var.set("Gain: --")
            return

        auto_exposure = self.cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)
        exposure = self.cap.get(cv2.CAP_PROP_EXPOSURE)
        gain = self.cap.get(cv2.CAP_PROP_GAIN)
        self.auto_exposure_var.set(f"Auto Exposure: {auto_exposure:.4f}")
        self.exposure_var.set(f"Exposure: {exposure:.4f}")
        self.gain_var.set(f"Gain: {gain:.4f}")

    def update_video(self):
        if self.cap is not None and self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret:
                self.last_frame = frame

        if self.last_frame is not None:
            rgb = cv2.cvtColor(self.last_frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb)
            imgtk = ImageTk.PhotoImage(image=image)
            self.video_panel.imgtk = imgtk
            self.video_panel.config(image=imgtk)

        self.root.after(FRAME_INTERVAL_MS, self.update_video)

    def on_close(self):
        self.release_camera()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = CameraProbeApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
