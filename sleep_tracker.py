import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont
from tkinter import messagebox
from tkinter import simpledialog
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
import imagingcontrol4 as ic4

CAPTURE_BACKEND_NAME = "DSHOW"
CAPTURE_BACKEND = cv2.CAP_DSHOW
LOCK_STATE_SEQUENCE = ("automatic", "unlocked", "locked")
LOCK_STATE_COLORS = {
    "automatic": ("Automatic", "blue", "white"),
    "unlocked": ("Unlocked", "green", "white"),
    "locked": ("Locked", "red", "white"),
}
LOCK_STATE_INACTIVE_BG = "#efefef"
LOCK_STATE_INACTIVE_FG = "#333333"


def normalize_lock_state(value):
    if value is None:
        return None
    state = str(value).strip().lower()
    return state if state in LOCK_STATE_SEQUENCE else None


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


def parse_optional_float(value):
    """Return float(value) unless the config entry is blank/missing."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return float(text)


def parse_optional_text(value):
    """Return stripped text unless the config entry is blank/missing."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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


class SessionClock:
    """Recorder-owned monotonic clock shared by the video and CSV outputs."""

    def __init__(self):
        self.start_time = None

    def start(self):
        self.start_time = time.monotonic()

    def elapsed(self, now=None) -> float:
        if self.start_time is None:
            return 0.0
        if now is None:
            now = time.monotonic()
        return max(0.0, now - self.start_time)

    def stamp(self, event_time=None) -> float:
        if self.start_time is None:
            return 0.0
        if event_time is None:
            event_time = time.monotonic()
        return max(0.0, event_time - self.start_time)


# Simulated Arduino for fallback when real one is not found
class SimulatedArduino:
    def __init__(self):
        self.in_waiting = False

    def readline(self):
        return b""

    def write(self, _data):
        return 0


class ImagingSourceQueueListener(ic4.QueueSinkListener):
    def __init__(self, buffer_count=6):
        super().__init__()
        self.buffer_count = buffer_count

    def sink_connected(self, sink, _image_type, min_buffers_required):
        sink.alloc_and_queue_buffers(max(self.buffer_count, min_buffers_required))
        return True

    def frames_queued(self, _sink):
        return


class ImagingSourceCapture:
    def __init__(self, device_info, capture_settings=None, logger=None):
        self.device_info = device_info
        self.capture_settings = capture_settings or {}
        self.logger = logger
        self._frame_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread = None
        self._latest_frame = None
        self._latest_frame_id = 0
        self._last_delivered_frame_id = 0
        self.grabber = ic4.Grabber()
        self.grabber.device_open(device_info)
        self._opened = True
        self._configure_device()
        self.listener = ImagingSourceQueueListener(buffer_count=6)
        self.sink = ic4.QueueSink(
            self.listener,
            accepted_pixel_formats=[ic4.PixelFormat.Mono8],
            max_output_buffers=1,
        )
        self.grabber.stream_setup(self.sink, setup_option=ic4.StreamSetupOption.ACQUISITION_START)
        self._worker_thread = threading.Thread(target=self._drain_queue_loop, daemon=True)
        self._worker_thread.start()

    def _log(self, message, level="DEBUG"):
        if self.logger is None:
            return
        self.logger(message, level=level)

    def _convert_buffer_to_frame(self, image):
        array = image.numpy_copy()
        if array.ndim == 3 and array.shape[2] == 1:
            array = array[:, :, 0]
        if array.ndim == 2:
            return cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
        return cv2.cvtColor(array, cv2.COLOR_RGB2BGR)

    def _drain_queue_loop(self):
        while not self._stop_event.is_set():
            if not self.isOpened():
                self._stop_event.wait(0.01)
                continue

            image = self.sink.try_pop_output_buffer()
            if image is None:
                self._stop_event.wait(0.001)
                continue

            try:
                frame = self._convert_buffer_to_frame(image)
            finally:
                image.release()

            with self._frame_lock:
                self._latest_frame = frame
                self._latest_frame_id += 1

    def _configure_device(self):
        prop_map = self.grabber.device_property_map
        user_set = self.capture_settings.get("UserSet")
        if user_set:
            try:
                prop_map.set_value(ic4.PropId.USER_SET_SELECTOR, str(user_set))
                prop_map.execute_command(ic4.PropId.USER_SET_LOAD)
                self._log(
                    f"Loaded UserSet '{user_set}' for camera {self.device_info.serial}",
                    level="DEBUG"
                )
            except Exception as exc:
                self._log(
                    f"Failed to load UserSet '{user_set}' on {self.device_info.serial}: {exc}",
                    level="WARNING"
                )
        try:
            prop_map.set_value(ic4.PropId.PIXEL_FORMAT, ic4.PixelFormat.Mono8)
        except Exception as exc:
            self._log(f"Failed to set Mono8 pixel format on {self.device_info.serial}: {exc}", level="WARNING")

        auto_exposure = self.capture_settings.get("AutoExposure")
        if auto_exposure is not None:
            self.set(cv2.CAP_PROP_AUTO_EXPOSURE, auto_exposure)
        exposure = self.capture_settings.get("Exposure")
        if exposure is not None and exposure >= 0:
            self.set(cv2.CAP_PROP_EXPOSURE, exposure)
        gain = self.capture_settings.get("Gain")
        if gain is not None:
            self.set(cv2.CAP_PROP_GAIN, gain)
        contrast = self.capture_settings.get("Contrast")
        if contrast is not None:
            self._log("Contrast requested but is not mapped in imagingcontrol4 backend; ignoring", level="WARNING")

    def isOpened(self):
        return self._opened and self.grabber is not None and self.grabber.is_device_open

    def read(self):
        if not self.isOpened():
            return False, None
        with self._frame_lock:
            if self._latest_frame is None or self._latest_frame_id == self._last_delivered_frame_id:
                return False, None
            frame = self._latest_frame.copy()
            self._last_delivered_frame_id = self._latest_frame_id
        return True, frame

    def get(self, prop_id):
        if not self.isOpened():
            return 0.0
        prop_map = self.grabber.device_property_map
        try:
            if prop_id == cv2.CAP_PROP_FRAME_WIDTH:
                return float(prop_map.get_value_int(ic4.PropId.WIDTH))
            if prop_id == cv2.CAP_PROP_FRAME_HEIGHT:
                return float(prop_map.get_value_int(ic4.PropId.HEIGHT))
            if prop_id == cv2.CAP_PROP_FPS:
                return float(prop_map.get_value_float(ic4.PropId.ACQUISITION_FRAME_RATE))
            if prop_id == cv2.CAP_PROP_AUTO_EXPOSURE:
                return 1.0 if prop_map.get_value_bool(ic4.PropId.EXPOSURE_AUTO) else -1.0
            if prop_id == cv2.CAP_PROP_EXPOSURE:
                return float(prop_map.get_value_float(ic4.PropId.EXPOSURE_TIME))
            if prop_id == cv2.CAP_PROP_GAIN:
                return float(prop_map.get_value_float(ic4.PropId.GAIN))
            if prop_id == cv2.CAP_PROP_CONTRAST:
                return 0.0
        except Exception:
            return 0.0
        return 0.0

    def get_limits(self, prop_id):
        if not self.isOpened():
            return None, None
        prop_map = self.grabber.device_property_map
        try:
            if prop_id == cv2.CAP_PROP_EXPOSURE:
                current = prop_map.get_value_float(ic4.PropId.EXPOSURE_TIME)
                prop_map.try_set_value_minimum(ic4.PropId.EXPOSURE_TIME)
                minimum = prop_map.get_value_float(ic4.PropId.EXPOSURE_TIME)
                prop_map.try_set_value_maximum(ic4.PropId.EXPOSURE_TIME)
                maximum = prop_map.get_value_float(ic4.PropId.EXPOSURE_TIME)
                prop_map.set_value(ic4.PropId.EXPOSURE_TIME, current)
                return float(minimum), float(maximum)
            if prop_id == cv2.CAP_PROP_GAIN:
                current = prop_map.get_value_float(ic4.PropId.GAIN)
                prop_map.try_set_value_minimum(ic4.PropId.GAIN)
                minimum = prop_map.get_value_float(ic4.PropId.GAIN)
                prop_map.try_set_value_maximum(ic4.PropId.GAIN)
                maximum = prop_map.get_value_float(ic4.PropId.GAIN)
                prop_map.set_value(ic4.PropId.GAIN, current)
                return float(minimum), float(maximum)
        except Exception:
            return None, None
        return None, None

    def set(self, prop_id, value):
        if not self.isOpened():
            return False
        prop_map = self.grabber.device_property_map
        try:
            if prop_id == cv2.CAP_PROP_AUTO_EXPOSURE:
                prop_map.set_value(ic4.PropId.EXPOSURE_AUTO, bool(value not in (0, 0.0, -1, -1.0, False)))
                return True
            if prop_id == cv2.CAP_PROP_EXPOSURE:
                prop_map.set_value(ic4.PropId.EXPOSURE_AUTO, False)
                prop_map.set_value(ic4.PropId.EXPOSURE_TIME, float(value))
                return True
            if prop_id == cv2.CAP_PROP_GAIN:
                try:
                    prop_map.set_value(ic4.PropId.GAIN_AUTO, False)
                except Exception:
                    pass
                prop_map.set_value(ic4.PropId.GAIN, float(value))
                return True
            if prop_id == cv2.CAP_PROP_CONTRAST:
                return False
        except Exception as exc:
            self._log(f"Failed to set property {prop_id} to {value} on {self.device_info.serial}: {exc}", level="WARNING")
            return False
        return False

    def release(self):
        if self.grabber is None:
            self._opened = False
            return
        self._stop_event.set()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
        try:
            if self.grabber.is_streaming:
                self.grabber.stream_stop()
        except Exception:
            pass
        self.listener = None
        self.sink = None
        self.grabber = None
        self.device_info = None
        with self._frame_lock:
            self._latest_frame = None
        self._opened = False

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
        config_section=None,
        capture_settings=None,
        capture_factory=None,
    ):
        self.logger = logger
        self._log("Initializing camera", level="DEBUG", suffix=f" {cam_id}")
        self.name = name
        self.config_section = config_section or name or str(cam_id)
        self.cam_id = cam_id
        self.com_port = com_port
        self.root_dir = root_dir
        self.flip_horizontal = flip_horizontal
        self.flip_vertical = flip_vertical
        self.capture_settings = capture_settings or {}
        if capture_factory is None:
            capture_factory = cv2.VideoCapture
        self.capture_factory = capture_factory
        self.capture_lock = threading.Lock()
        self.cap = self.capture_factory(cam_id)
        self._log(self.describe_capture(), level="DEBUG")
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
        self.session_clock = None
        self.mouse_id = ""
        self.session_duration = 0  # in minutes
        self.exp_id = ""
        self.lock_state = "automatic"
        self.lock_state_synced_from_hardware = False
        self.lock_state_user_overridden = False
        self.latest_status = None
        self.last_arduino_line = ""
        self.last_logged_arduino_line = ""
        self.frame_lock = threading.Lock()
        self.latest_frame = None
        self.latest_frame_time = None
        self.last_frame_ms = 0.0
        self.last_serial_ms = 0.0
        self.serial_lock = threading.Lock()
        self.last_write_time = None
        self.last_write_fps = None
        self.last_stall_warning_time = None
        self.last_written_frame_time = None
        self.repeated_frame_write_count = 0
        self.last_read_complete_time = None
        self.last_effective_fps = None
        self.record_interval_s = 0.1
        self.recording_thread = None
        self.recording_stop_event = threading.Event()
        self.property_limits = {}

    def describe_capture(self):
        with self.capture_lock:
            if self.cap is None:
                return f"{self.name or self.cam_id}: capture not created"
            opened = self.cap.isOpened()
            width = self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)
            height = self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
            fps = self.cap.get(cv2.CAP_PROP_FPS)
        return (
            f"{self.name or self.cam_id}: backend={CAPTURE_BACKEND_NAME}, "
            f"opened={opened}, size={int(width)}x{int(height)}, fps={fps:.2f}"
        )

    def reset_fps_diagnostic(self):
        self.last_read_complete_time = None
        self.last_effective_fps = None

    def replace_capture(self, cap, description=None):
        with self.capture_lock:
            if self.cap is not None and self.cap is not cap:
                self.cap.release()
            self.cap = cap
        with self.frame_lock:
            self.latest_frame = None
            self.latest_frame_time = None
        self.reset_fps_diagnostic()
        if description:
            self._log(f"{self.name or self.cam_id}: {description}", level="INFO")
        self._log(self.describe_capture(), level="DEBUG")

    def release_capture(self):
        self.stop_background_recording()
        with self.capture_lock:
            if self.cap is not None:
                self.cap.release()
            self.cap = None
        with self.frame_lock:
            self.latest_frame = None
            self.latest_frame_time = None
        self.reset_fps_diagnostic()

    def _log(self, message, level="INFO", suffix=""):
        if self.logger:
            self.logger(message + suffix, level=level)
        else:
            if level == "DEBUG":
                print(f"{message}{suffix}")
            else:
                print(f"[{level}] {message}{suffix}")

    def start_recording(self, mouse_id, session_duration, exp_id, record_fps=10.0):
        self.mouse_id = mouse_id
        self.session_duration = session_duration
        self.record_fps = max(record_fps, 0.1)
        self.record_interval_s = 1.0 / self.record_fps
        self.session_clock = SessionClock()
        self.session_clock.start()
        self.last_write_time = None
        self.last_write_fps = None
        self.last_stall_warning_time = None
        self.last_written_frame_time = None
        self.repeated_frame_write_count = 0
        self.elapsed_time = 0
        output_id = self.name or f"camera_{self.cam_id}"
        video_path, csv_path = generate_file_paths(mouse_id, exp_id, output_id, self.root_dir)
        meta_path = os.path.join(os.path.dirname(video_path), f"{exp_id}_meta.txt")
        with open(meta_path, "w", newline="") as meta_file:
            meta_file.write(self.name or "")
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.writer = cv2.VideoWriter(video_path, fourcc, self.record_fps, (width, height))
        self.csv_file = open(csv_path, 'w', newline='')
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(['timestamp', 'arduino_data'])
        self.recording = True
        self.recording_stop_event.clear()
        self.recording_thread = threading.Thread(target=self.recording_loop, daemon=True)
        self.recording_thread.start()

    def stop_recording(self):
        self.recording = False
        self.stop_background_recording()
        if self.writer:
            self.writer.release()
            self.writer = None
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = None
        self.csv_writer = None
        self.session_clock = None
        self.last_written_frame_time = None
        self.repeated_frame_write_count = 0

    def stop_background_recording(self):
        self.recording_stop_event.set()
        if self.recording_thread is not None and self.recording_thread.is_alive() and threading.current_thread() is not self.recording_thread:
            self.recording_thread.join(timeout=2.0)
        self.recording_thread = None

    def recording_loop(self):
        while not self.recording_stop_event.is_set() and self.recording:
            loop_start = time.monotonic()
            self.write_latest_frame()
            elapsed = time.monotonic() - loop_start
            sleep_s = self.record_interval_s - elapsed
            if sleep_s > 0:
                self.recording_stop_event.wait(sleep_s)

    def _log_frame_stall_warning(self, message):
        now = time.monotonic()
        cooldown_s = 5.0
        if self.last_stall_warning_time is not None and (now - self.last_stall_warning_time) < cooldown_s:
            return False
        self.last_stall_warning_time = now
        self._log(message, level="WARNING")
        return True

    def apply_flips(self, frame):
        if self.flip_horizontal:
            frame = cv2.flip(frame, 1)
        if self.flip_vertical:
            frame = cv2.flip(frame, 0)
        return frame

    def annotate_frame(self, frame, read_complete):
        return frame

    def poll_capture(self):
        frame_start = time.monotonic()
        with self.capture_lock:
            if self.cap is None:
                return False
            ret, frame = self.cap.read()
        read_complete = time.monotonic()
        self.last_frame_ms = (read_complete - frame_start) * 1000.0
        self.update_fps_diagnostic(read_complete, ret)
        arduino_data = ""
        serial_start = time.monotonic()
        with self.serial_lock:
            while self.serial.in_waiting:
                arduino_data = self.serial.readline().decode().strip()
            if arduino_data:
                self.latest_status = self.parse_arduino_status(arduino_data)
                self.last_arduino_line = arduino_data
        self.last_serial_ms = (time.monotonic() - serial_start) * 1000.0
        if ret:
            frame = self.apply_flips(frame)
            frame = self.annotate_frame(frame, read_complete)
            with self.frame_lock:
                self.latest_frame = frame.copy()
                self.latest_frame_time = read_complete
            self.sync_lock_state_from_status()
            return True
        return False

    def get_latest_frame(self):
        with self.frame_lock:
            if self.latest_frame is None:
                return None
            return self.latest_frame.copy()

    def get_latest_frame_packet(self):
        with self.frame_lock:
            if self.latest_frame is None:
                return None, None
            return self.latest_frame.copy(), self.latest_frame_time

    def write_latest_frame(self):
        if not self.recording or self.writer is None or self.csv_writer is None:
            return False

        frame, frame_time = self.get_latest_frame_packet()
        now = time.monotonic()
        if frame is None:
            self._log_frame_stall_warning(
                f"{self.name or self.cam_id}: no video frame available for recording; the camera may be disconnected or stalled."
            )
            return False

        timestamp = self.session_clock.stamp() if self.session_clock is not None else 0.0
        self.writer.write(frame)
        stall_threshold = max(1.0, 3.0 * self.record_interval_s)
        if frame_time is not None:
            frame_age = now - frame_time
            if self.last_written_frame_time is not None and frame_time <= self.last_written_frame_time:
                self.repeated_frame_write_count += 1
            else:
                self.repeated_frame_write_count = 0
            if frame_age >= stall_threshold:
                if self.repeated_frame_write_count >= 3:
                    self._log_frame_stall_warning(
                        f"{self.name or self.cam_id}: repeated video frame timestamps detected for {self.repeated_frame_write_count} consecutive writes (frame age {frame_age:.2f}s, threshold {stall_threshold:.2f}s). The camera may be stalled or the buffer is not advancing."
                    )
                else:
                    self._log_frame_stall_warning(
                        f"{self.name or self.cam_id}: latest video frame is {frame_age:.2f}s old while recording (threshold {stall_threshold:.2f}s). The camera may be stalled or dropping frames."
                    )
        else:
            self.repeated_frame_write_count = 0
        self.last_written_frame_time = frame_time
        if self.last_write_time is not None:
            delta = now - self.last_write_time
            if delta > 0:
                self.last_write_fps = 1.0 / delta
        self.last_write_time = now
        self.csv_writer.writerow([timestamp, self.last_arduino_line])
        self.elapsed_time = int(timestamp)
        return True

    def update_fps_diagnostic(self, read_complete, ret):
        if not ret:
            return

        if self.last_read_complete_time is None:
            self.last_read_complete_time = read_complete
            return

        delta = read_complete - self.last_read_complete_time
        self.last_read_complete_time = read_complete
        if delta <= 0:
            return

        self.last_effective_fps = 1.0 / delta

    def parse_arduino_status(self, line):
        parts = [part.strip() for part in line.split(";")]
        if len(parts) != 3:
            return None
        brake_raw, wheel_pos, mode = parts
        brake_text = "locked" if brake_raw == "0" else "unlocked" if brake_raw == "1" else brake_raw
        return (brake_text, wheel_pos, normalize_lock_state(mode) or mode)

    def sync_lock_state_from_status(self):
        if self.lock_state_synced_from_hardware or self.lock_state_user_overridden or self.latest_status is None:
            return False
        mode = normalize_lock_state(self.latest_status[2])
        if mode is None:
            return False
        changed = self.lock_state != mode
        self.lock_state = mode
        self.lock_state_synced_from_hardware = True
        return changed

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
        self.record_fps = 10.0
        self.frame_interval_ms = int(1000.0 / self.record_fps)
        self.acquire_sleep_s = 0.001
        self.acquisition_stop_event = threading.Event()
        self.acquisition_thread = None

        self.status_dialog = None
        self.status_text = None
        self.create_status_dialog()
        self.log("Starting application", level="DEBUG")
        self.initialization_failed = False
        self.initialize_imaging_source()
        self.load_config()
        self.log("Configuration loaded", level="DEBUG")
        if self.initialization_failed:
            self.close_status_dialog()
            return
        self.start_acquisition_loop()
        self.build_gui()
        self.log("GUI built", level="DEBUG")
        self.close_status_dialog()
        self.update_video()
        self.auto_cycle_loop()

    def load_config(self):
        self.log("Reading configuration file...", level="DEBUG")
        self.config_path = 'configuration.txt'
        config = configparser.ConfigParser()
        config.read(self.config_path)
        self.log("Configuration file loaded.", level="DEBUG")
        self.root_dir = config['DEFAULT']['RootDirectory']
        self.remote_repo = config['DEFAULT'].get('RemoteRepository', r'\\ar-lab-nas1\\DataServer\\Remote_Repository')
        self.exp_list_dir = config['DEFAULT'].get('ExperimentListDirectory', r'\\ar-lab-nas1\\DataServer\\Remote_Repository\\habituation')
        self.log(f"Checking root directory: {self.root_dir}", level="DEBUG")
        if not os.path.exists(self.root_dir):
            self.log(f"Root directory '{self.root_dir}' not found. Creating it.", level="INFO")
            os.makedirs(self.root_dir)

        section_names = config.sections()
        configured_camera_ids = []
        for section_name in section_names:
            if "CameraID" not in config[section_name]:
                continue
            try:
                configured_camera_ids.append(self.parse_camera_id(config[section_name]["CameraID"]))
            except Exception:
                pass
        self.camera_id_mode = self.determine_camera_id_mode(configured_camera_ids)

        for section_name in section_names:
            if section_name.upper() == "DEFAULT":
                continue
            if "CameraID" not in config[section_name]:
                continue
            raw_device_serial = config[section_name].get("DeviceSerial", "").strip()
            if raw_device_serial:
                cam_id = raw_device_serial
            else:
                cam_id = self.parse_camera_id(config[section_name]["CameraID"])
            com_port = config[section_name]["COMPort"]
            capture_settings = self.parse_capture_settings(config[section_name], section_name)
            self.log(f"{section_name}: Checking camera {cam_id} and COM port {com_port}", level="DEBUG")

            cap = self.open_capture(cam_id, capture_settings=capture_settings)
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
                config_section=section_name,
                capture_settings=capture_settings,
                capture_factory=lambda camera_id, settings=capture_settings: self.open_capture(
                    camera_id,
                    capture_settings=settings
                )
            )
            self.setups.append(setup)
            self.log(f"{section_name} initialized.", level="DEBUG")

        if not self.setups:
            self.log("No valid camera setups found. Exiting.", level="ERROR")
            self.initialization_failed = True
            self.root.quit()

    def parse_capture_settings(self, section, section_name):
        settings = {}
        settings["UserSet"] = parse_optional_text(section.get("UserSet"))
        for option in ("AutoExposure", "Exposure", "Gain", "Contrast"):
            try:
                settings[option] = parse_optional_float(section.get(option))
            except ValueError:
                self.log(
                    f"{section_name}: invalid {option} value '{section.get(option)}'; ignoring",
                    level="WARNING"
                )
                settings[option] = None
        return settings

    def initialize_imaging_source(self):
        ic4.Library.init()
        self.ic4_device_infos = list(ic4.DeviceEnum.devices())
        self.log(
            f"Imaging Source devices: {[(device.model_name, device.serial) for device in self.ic4_device_infos]}",
            level="DEBUG"
        )

    def determine_camera_id_mode(self, configured_camera_ids):
        numeric_ids = [cam_id for cam_id in configured_camera_ids if isinstance(cam_id, int)]
        if not numeric_ids:
            return "serial"
        if (
            self.ic4_device_infos
            and min(numeric_ids) >= 1
            and max(numeric_ids) <= len(self.ic4_device_infos)
            and 0 not in numeric_ids
        ):
            return "one_based_index"
        return "zero_based_index"

    def resolve_imaging_source_device(self, cam_id):
        if isinstance(cam_id, int):
            if self.camera_id_mode == "one_based_index":
                index = cam_id - 1
            else:
                index = cam_id
            if index < 0 or index >= len(self.ic4_device_infos):
                raise IndexError(f"CameraID {cam_id} did not resolve to an Imaging Source device")
            return self.ic4_device_infos[index]

        text = str(cam_id).strip()
        for device in self.ic4_device_infos:
            if device.serial == text:
                return device
        for device in self.ic4_device_infos:
            label = f"{device.model_name} {device.serial}".strip()
            if label == text or device.model_name == text:
                return device
        raise LookupError(f"Could not resolve Imaging Source device '{cam_id}'")

    def start_acquisition_loop(self):
        self.acquisition_stop_event.clear()
        self.acquisition_thread = threading.Thread(target=self.acquisition_loop, daemon=True)
        self.acquisition_thread.start()

    def stop_acquisition_loop(self):
        self.acquisition_stop_event.set()
        if self.acquisition_thread is not None and self.acquisition_thread.is_alive():
            self.acquisition_thread.join(timeout=2.0)
        self.acquisition_thread = None

    def acquisition_loop(self):
        while not self.acquisition_stop_event.is_set():
            did_work = False
            for setup in self.setups:
                if self.acquisition_stop_event.is_set():
                    break
                self.ensure_setup_capture(setup)
                if setup.poll_capture():
                    did_work = True
            if not did_work:
                self.acquisition_stop_event.wait(self.acquire_sleep_s)

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

    def open_capture(self, cam_id, capture_settings=None):
        return self.open_capture_with_fallback(cam_id, capture_settings=capture_settings, log_success=True)

    def open_capture_with_fallback(self, cam_id, capture_settings=None, log_success=True):
        try:
            device_info = self.resolve_imaging_source_device(cam_id)
            cap = ImagingSourceCapture(device_info, capture_settings=capture_settings, logger=self.log)
        except Exception as exc:
            self.log(f"Failed to open Imaging Source camera {cam_id}: {exc}", level="WARNING")
            class _ClosedCapture:
                def isOpened(self):
                    return False
                def get(self, _prop_id):
                    return 0.0
                def set(self, _prop_id, _value):
                    return False
                def read(self):
                    return False, None
                def release(self):
                    return None
            cap = _ClosedCapture()
        if log_success:
            if cap.isOpened():
                serial = getattr(getattr(cap, "device_info", None), "serial", "unknown")
                self.log(f"Opened camera {cam_id} with Imaging Source backend (serial={serial})", level="DEBUG")
                self.log(self.describe_capture_properties(cap, cam_id), level="DEBUG")
            else:
                self.log(f"Failed to open camera {cam_id} with Imaging Source backend", level="WARNING")
        return cap

    def open_capture_default(self, cam_id, capture_settings=None, log_success=True):
        return self.open_capture_with_fallback(cam_id, capture_settings=capture_settings, log_success=log_success)

    def ensure_setup_capture(self, setup):
        if setup.cap is not None and setup.cap.isOpened():
            return
        self.log(f"{setup.name}: reopening released camera {setup.cam_id}", level="INFO")
        setup.replace_capture(
            self.open_capture(setup.cam_id, capture_settings=setup.capture_settings),
            description="capture reopened"
        )

    def describe_capture_properties(self, cap, cam_id):
        opened = cap.isOpened()
        width = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        fps = cap.get(cv2.CAP_PROP_FPS)
        auto_exposure = cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)
        exposure = cap.get(cv2.CAP_PROP_EXPOSURE)
        gain = cap.get(cv2.CAP_PROP_GAIN)
        contrast = cap.get(cv2.CAP_PROP_CONTRAST)
        return (
            f"Camera {cam_id}: opened={opened}, size={int(width)}x{int(height)}, "
            f"fps={fps:.2f}, auto_exposure={auto_exposure:.4f}, exposure={exposure:.4f}, "
            f"gain={gain:.4f}, contrast={contrast:.4f}"
        )

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

        media_frame = ttk.Frame(self.root)
        media_frame.pack(fill="x", padx=10, pady=10)
        media_frame.grid_columnconfigure(0, weight=0)
        media_frame.grid_columnconfigure(1, weight=1)

        button_style = ttk.Style(self.root)
        button_style.configure("App.Big.TButton", padding=(12, 7))
        button_style.configure("App.Big.TCheckbutton", padding=(10, 5))
        button_style.configure("App.Compact.TButton", padding=(5, 2))

        tune_frame = ttk.LabelFrame(media_frame, text="Camera Tuning")
        tune_frame.grid(row=0, column=0, sticky="nw", padx=(0, 8))
        self.camera_settings_visible = True

        self.camera_settings_toggle_button = ttk.Button(
            tune_frame,
            text="Hide Settings",
            command=self.toggle_camera_settings_visibility,
            style="App.Compact.TButton",
        )
        self.camera_settings_toggle_button.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=(6, 3))

        self.camera_settings_frame = ttk.Frame(tune_frame)
        self.camera_settings_frame.grid(row=1, column=0, columnspan=2, sticky="nw", padx=4, pady=(0, 6))

        ttk.Label(self.camera_settings_frame, text="Exposure").grid(row=0, column=0, sticky="w", pady=(10, 0))
        self.exposure_value_entry = ttk.Entry(self.camera_settings_frame, width=12)
        self.exposure_value_entry.grid(row=1, column=0, sticky="ew")
        self.exposure_apply_button = ttk.Button(self.camera_settings_frame, text="Apply Exposure", command=lambda: self.apply_value_from_entry("Exposure"), style="App.Big.TButton")
        self.exposure_apply_button.grid(row=1, column=1, padx=(8, 0), sticky="ew")
        self.exposure_range_label = ttk.Label(self.camera_settings_frame, text="Range: --", font=self.small_font)
        self.exposure_range_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        ttk.Label(self.camera_settings_frame, text="Gain").grid(row=3, column=0, sticky="w", pady=(14, 0))
        self.gain_value_entry = ttk.Entry(self.camera_settings_frame, width=12)
        self.gain_value_entry.grid(row=4, column=0, sticky="ew")
        self.gain_apply_button = ttk.Button(self.camera_settings_frame, text="Apply Gain", command=lambda: self.apply_value_from_entry("Gain"), style="App.Big.TButton")
        self.gain_apply_button.grid(row=4, column=1, padx=(8, 0), sticky="ew")
        self.gain_range_label = ttk.Label(self.camera_settings_frame, text="Range: --", font=self.small_font)
        self.gain_range_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.refresh_camera_settings_button = ttk.Button(self.camera_settings_frame, text="Refresh Camera Values", command=self.update_camera_settings_label, style="App.Big.TButton")
        self.refresh_camera_settings_button.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        self.camera_settings_label = ttk.Label(self.camera_settings_frame, text="Exposure: -- | Gain: --", font=self.small_font, cursor="hand2")
        self.camera_settings_label.grid(row=7, column=0, columnspan=2, sticky="w", pady=(15, 0))
        self.camera_settings_label.bind("<Double-Button-1>", self.prompt_set_exposure_and_gain)

        self.video_panel = ttk.Label(media_frame)
        self.video_panel.grid(row=0, column=1, sticky="")

        self.fps_label = ttk.Label(self.root, text="FPS: --", font=self.small_font)
        self.fps_label.pack()

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

        self.start_button = ttk.Button(button_frame, text="Start", command=self.start_recording, style="App.Big.TButton")
        self.start_button.grid(row=0, column=0)
        self.stop_button = ttk.Button(button_frame, text="Stop", command=self.stop_recording, style="App.Big.TButton")
        self.stop_button.grid(row=0, column=1)
        self.left_button = ttk.Button(button_frame, text="<", command=self.prev_setup, style="App.Big.TButton")
        self.left_button.grid(row=0, column=2)
        self.right_button = ttk.Button(button_frame, text=">", command=self.next_setup, style="App.Big.TButton")
        self.right_button.grid(row=0, column=3)
        self.test_button = ttk.Button(button_frame, text="Test system", command=self.start_test_system, style="App.Big.TButton")
        self.test_button.grid(row=0, column=4, padx=(10, 0))

        self.auto_cycle_var = tk.BooleanVar()
        self.auto_cycle_button = ttk.Checkbutton(button_frame, text="Auto Cycle", variable=self.auto_cycle_var, command=self.toggle_auto_cycle, style="App.Big.TCheckbutton")
        self.auto_cycle_button.grid(row=0, column=5)

        self.dwell_label = ttk.Label(button_frame, text="Dwell (s):")
        self.dwell_label.grid(row=0, column=6)
        self.dwell_entry = ttk.Entry(button_frame, width=5)
        self.dwell_entry.insert(0, "5")
        self.dwell_entry.grid(row=0, column=7)

        self.lock_state_control_frame = ttk.LabelFrame(button_frame, text="Lock mode")
        self.lock_state_control_frame.grid(row=1, column=0, columnspan=8, pady=(10, 0), sticky="ew")
        for idx in range(3):
            self.lock_state_control_frame.columnconfigure(idx, weight=1)

        self.lock_state_buttons = {}
        for idx, state in enumerate(LOCK_STATE_SEQUENCE):
            label, _bg, _fg = LOCK_STATE_COLORS[state]
            button = tk.Button(
                self.lock_state_control_frame,
                text=label,
                command=lambda s=state: self.set_lock_state(s),
                padx=8,
                pady=3,
                relief="raised",
                bd=1,
                highlightthickness=0,
                takefocus=False,
            )
            button.grid(row=0, column=idx, padx=3, pady=2, sticky="ew")
            self.lock_state_buttons[state] = button

        self.fps_label_entry = ttk.Label(button_frame, text="FPS:")
        self.fps_label_entry.grid(row=2, column=0, sticky="e", pady=(10, 0))
        self.fps_entry = ttk.Entry(button_frame, width=6)
        self.fps_entry.insert(0, str(self.record_fps))
        self.fps_entry.grid(row=2, column=1, pady=(10, 0))
        self.fps_apply_button = ttk.Button(button_frame, text="Apply FPS", command=self.apply_fps, style="App.Big.TButton")
        self.fps_apply_button.grid(row=2, column=2, columnspan=2, padx=(5, 0), pady=(10, 0))

        self.debug_var = tk.BooleanVar()
        self.debug_checkbox = ttk.Checkbutton(button_frame, text="Debug", variable=self.debug_var)
        self.debug_checkbox.grid(row=1, column=8, padx=(10, 0), pady=(10, 0))
        self.update_setup_label()
        self.update_lock_state_button()
        self._update_camera_settings_toggle_button()
        self.update_camera_settings_label()
        self.last_display_time = None
        self.last_display_fps = None

    def update_video(self):
        setup = self.setups[self.current_setup]
        now = time.monotonic()
        if self.last_display_time is not None:
            delta = now - self.last_display_time
            if delta > 0:
                self.last_display_fps = 1.0 / delta
        self.last_display_time = now
        frame = setup.get_latest_frame()
        if self.debug_var.get():
            self.debug_log(
                f"{setup.name}: frame_read={setup.last_frame_ms:.1f}ms, "
                f"serial_drain={setup.last_serial_ms:.1f}ms, "
                f"effective_fps={setup.last_effective_fps if setup.last_effective_fps is not None else float('nan'):.2f}, "
                f"frame_ok={frame is not None}"
            )
        if frame is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            imgtk = ImageTk.PhotoImage(image=img)
            self.video_panel.imgtk = imgtk
            self.video_panel.config(image=imgtk)
        if setup.recording and setup.last_arduino_line and setup.last_arduino_line != setup.last_logged_arduino_line:
            self.debug_log(f"{setup.name}: {setup.last_arduino_line}")
            setup.last_logged_arduino_line = setup.last_arduino_line

        self.update_lock_state_button()

        elapsed = 0
        if setup.recording and setup.session_clock is not None:
            elapsed = int(setup.session_clock.elapsed())
            setup.elapsed_time = elapsed
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
        fps_parts = []
        if setup.last_effective_fps is not None:
            fps_parts.append(f"Acquire FPS: {setup.last_effective_fps:.1f}")
        if setup.last_write_fps is not None:
            fps_parts.append(f"Write FPS: {setup.last_write_fps:.1f}")
        self.fps_label.config(text=" | ".join(fps_parts) if fps_parts else "FPS: --")

        if self.running:
            self.root.after(self.frame_interval_ms, self.update_video)

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

    def apply_fps(self):
        try:
            fps = float(self.fps_entry.get())
        except ValueError:
            messagebox.showerror("Invalid FPS", "Please enter a valid number for FPS.")
            return
        if fps <= 0:
            messagebox.showerror("Invalid FPS", "FPS must be greater than 0.")
            return
        self.record_fps = fps
        self.frame_interval_ms = int(1000.0 / fps)
        if any(setup.recording for setup in self.setups):
            messagebox.showinfo("FPS Updated", "Display FPS updated now. Video FPS will apply on next recording.")

    def toggle_camera_settings_visibility(self):
        self.camera_settings_visible = not self.camera_settings_visible
        if self.camera_settings_visible:
            self.camera_settings_frame.grid()
        else:
            self.camera_settings_frame.grid_remove()
        self._update_camera_settings_toggle_button()

    def _update_camera_settings_toggle_button(self):
        if hasattr(self, "camera_settings_toggle_button"):
            self.camera_settings_toggle_button.config(
                text="Hide Settings" if self.camera_settings_visible else "Show Settings"
            )

    def persist_setup_capture_settings(self, setup):
        config = configparser.ConfigParser()
        config.read(self.config_path)
        if setup.config_section not in config:
            config[setup.config_section] = {}
        section = config[setup.config_section]
        for key, value in setup.capture_settings.items():
            if value is None:
                if key in section:
                    section[key] = ""
            else:
                if isinstance(value, str):
                    section[key] = value
                else:
                    numeric = float(value)
                    if numeric.is_integer():
                        section[key] = str(int(numeric))
                    else:
                        section[key] = str(numeric)
        with open(self.config_path, "w") as config_file:
            config.write(config_file)

    def update_camera_settings_label(self):
        setup = self.setups[self.current_setup]
        exposure = setup.capture_settings.get("Exposure")
        gain = setup.capture_settings.get("Gain")
        exposure_limits = setup.property_limits.get("Exposure", (None, None))
        gain_limits = setup.property_limits.get("Gain", (None, None))
        with setup.capture_lock:
            if setup.cap is not None and setup.cap.isOpened():
                exposure = setup.cap.get(cv2.CAP_PROP_EXPOSURE)
                gain = setup.cap.get(cv2.CAP_PROP_GAIN)
                setup.capture_settings["Exposure"] = exposure
                setup.capture_settings["Gain"] = gain
        exposure_text = "--" if exposure is None else f"{exposure:.4f}"
        gain_text = "--" if gain is None else f"{gain:.4f}"
        self.camera_settings_label.config(text=f"Exposure: {exposure_text} | Gain: {gain_text}")
        self.exposure_value_entry.delete(0, tk.END)
        self.exposure_value_entry.insert(0, "" if exposure is None else f"{exposure:.4f}")
        self.gain_value_entry.delete(0, tk.END)
        self.gain_value_entry.insert(0, "" if gain is None else f"{gain:.4f}")
        exp_min, exp_max = exposure_limits
        gain_min, gain_max = gain_limits
        self.exposure_range_label.config(
            text="Range: --" if exp_min is None or exp_max is None else f"Range: {exp_min:.4f} to {exp_max:.4f}"
        )
        self.gain_range_label.config(
            text="Range: --" if gain_min is None or gain_max is None else f"Range: {gain_min:.4f} to {gain_max:.4f}"
        )

    def apply_value_from_entry(self, setting_name):
        entry = self.exposure_value_entry if setting_name == "Exposure" else self.gain_value_entry
        try:
            requested_value = float(entry.get())
        except ValueError:
            messagebox.showerror("Invalid Value", f"Enter a numeric {setting_name.lower()} value.")
            return
        self.set_current_camera_setting(setting_name, requested_value)

    def set_current_camera_setting(self, setting_name, requested_value):
        setup = self.setups[self.current_setup]
        self.ensure_setup_capture(setup)
        prop_map = {
            "Exposure": cv2.CAP_PROP_EXPOSURE,
            "Gain": cv2.CAP_PROP_GAIN,
        }
        with setup.capture_lock:
            if setup.cap is None or not setup.cap.isOpened():
                messagebox.showerror("Camera Unavailable", f"{setup.name}: camera is not open.")
                return None
            if setting_name == "Exposure":
                setup.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, -1)
                setup.capture_settings["AutoExposure"] = -1.0
            ok = setup.cap.set(prop_map[setting_name], requested_value)
            applied_value = setup.cap.get(prop_map[setting_name])

        if not ok:
            self.log(
                f"{setup.name}: failed to set {setting_name} to {requested_value:.4f}; reported {applied_value:.4f}",
                level="WARNING"
            )
        else:
            self.log(
                f"{setup.name}: set {setting_name} target={requested_value:.4f}, reported={applied_value:.4f}",
                level="INFO"
            )

        setup.capture_settings[setting_name] = applied_value
        self.persist_setup_capture_settings(setup)
        self.update_camera_settings_label()
        return applied_value

    def prompt_set_exposure_and_gain(self, _event=None):
        setup = self.setups[self.current_setup]
        self.ensure_setup_capture(setup)
        with setup.capture_lock:
            if setup.cap is None or not setup.cap.isOpened():
                messagebox.showerror("Camera Unavailable", f"{setup.name}: camera is not open.")
                return
            current_exposure = setup.cap.get(cv2.CAP_PROP_EXPOSURE)
            current_gain = setup.cap.get(cv2.CAP_PROP_GAIN)

        requested_exposure = simpledialog.askfloat(
            "Set Exposure",
            f"{setup.name}: enter exposure value",
            initialvalue=current_exposure,
            parent=self.root,
        )
        if requested_exposure is None:
            return

        applied_exposure = self.set_current_camera_setting("Exposure", requested_exposure)
        if applied_exposure is None:
            return

        requested_gain = simpledialog.askfloat(
            "Set Gain",
            f"{setup.name}: enter gain value",
            initialvalue=current_gain,
            parent=self.root,
        )
        if requested_gain is None:
            return

        self.set_current_camera_setting("Gain", requested_gain)

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
        self.start_setup_recording(setup, mouse_id, session_duration, exp_id)
        self.update_setup_label()

    def stop_recording(self):
        self.setups[self.current_setup].stop_recording()

    def start_setup_recording(self, setup, mouse_id, session_duration, exp_id):
        setup.exp_id = exp_id
        setup.start_recording(mouse_id, session_duration, exp_id, record_fps=self.record_fps)
        setup.sync_lock_state_from_status()
        self.update_lock_state_button()
        setup.send_lock_state(log_fn=self.debug_log)

    def start_test_system(self):
        mouse_id = "TEST"
        session_duration = 600
        self.mouse_id_entry.delete(0, tk.END)
        self.mouse_id_entry.insert(0, mouse_id)
        self.duration_entry.delete(0, tk.END)
        self.duration_entry.insert(0, str(session_duration))

        exp_ids = []
        for setup in self.setups:
            exp_id, _remote_exp_dir = self.generate_and_register_exp(mouse_id)
            self.ensure_setup_capture(setup)
            self.start_setup_recording(setup, mouse_id, session_duration, exp_id)
            exp_ids.append(f"{setup.name or setup.cam_id}={exp_id}")
        self.log(
            (
                f"Started test-system acquisition on {len(self.setups)} setups for "
                f"{session_duration} minutes with expIDs: {', '.join(exp_ids)}."
            ),
            level="WARNING"
        )
        self.update_setup_label()

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
        setup.sync_lock_state_from_status()
        state = normalize_lock_state(setup.lock_state) or "automatic"
        for candidate_state, button in getattr(self, "lock_state_buttons", {}).items():
            label, _bg, _fg = LOCK_STATE_COLORS[candidate_state]
            if candidate_state == state:
                active_bg, active_fg = LOCK_STATE_COLORS[state][1:]
                button.config(
                    text=label,
                    bg=active_bg,
                    fg=active_fg,
                    relief="sunken",
                    bd=3,
                    activebackground=active_bg,
                    activeforeground=active_fg,
                )
            else:
                button.config(
                    text=label,
                    bg=LOCK_STATE_INACTIVE_BG,
                    fg=LOCK_STATE_INACTIVE_FG,
                    relief="raised",
                    bd=2,
                    activebackground=LOCK_STATE_INACTIVE_BG,
                    activeforeground=LOCK_STATE_INACTIVE_FG,
                )

    def set_lock_state(self, state):
        setup = self.setups[self.current_setup]
        normalized_state = normalize_lock_state(state) or "automatic"
        setup.lock_state_user_overridden = True
        setup.lock_state_synced_from_hardware = True
        setup.lock_state = normalized_state
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
        entries = []
        for index, device in enumerate(self.ic4_device_infos, start=1 if self.camera_id_mode == "one_based_index" else 0):
            entries.append({
                "id": index,
                "path": f"{device.model_name} / {device.serial}",
            })
        return entries

    def show_camera_streams(self):
        if hasattr(self, "camera_viewer") and self.camera_viewer is not None:
            if self.camera_viewer.winfo_exists():
                self.camera_viewer.lift()
                return

        self.camera_viewer = tk.Toplevel(self.root)
        self.camera_viewer.title("Configured Cameras")
        self.camera_viewer.geometry("1000x700")
        self.camera_viewer.transient(self.root)

        if not self.setups:
            ttk.Label(self.camera_viewer, text="No cameras found.").pack(padx=20, pady=20)
            return

        self.camera_viewer_items = []
        columns = 2
        for idx, setup in enumerate(self.setups):
            frame = ttk.Frame(self.camera_viewer)
            row = idx // columns
            col = idx % columns
            frame.grid(row=row, column=col, padx=10, pady=10, sticky="nsew")
            self.camera_viewer.grid_columnconfigure(col, weight=1)
            self.camera_viewer.grid_rowconfigure(row, weight=1)

            label_text = f"{setup.name or f'Device {setup.cam_id}'}\nCamera ID: {setup.cam_id}"
            ttk.Label(frame, text=label_text, justify="center", wraplength=480).pack()
            panel = ttk.Label(frame)
            panel.pack(fill="both", expand=True)
            self.camera_viewer_items.append({"setup": setup, "panel": panel})

        self.camera_viewer.protocol("WM_DELETE_WINDOW", self.close_camera_viewer)
        self.update_camera_viewer()

    def update_camera_viewer(self):
        if not hasattr(self, "camera_viewer") or self.camera_viewer is None:
            return
        if not self.camera_viewer.winfo_exists():
            return
        for item in self.camera_viewer_items:
            setup = item["setup"]
            panel = item["panel"]
            frame = setup.get_latest_frame()
            if frame is not None:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                imgtk = ImageTk.PhotoImage(image=img)
                panel.imgtk = imgtk
                panel.config(image=imgtk)
        self.camera_viewer.after(100, self.update_camera_viewer)

    def close_camera_viewer(self):
        if hasattr(self, "camera_viewer_items"):
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
        self._update_camera_settings_toggle_button()
        self.update_camera_settings_label()

    def on_closing(self):
        self.running = False
        self.stop_acquisition_loop()
        for setup in self.setups:
            setup.stop_recording()
            setup.release_capture()
        self.ic4_device_infos = []
        ic4.Library.exit()
        self.root.destroy()

if __name__ == '__main__':
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()
