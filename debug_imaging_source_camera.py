import argparse
import configparser
import math
import statistics
import time

import imagingcontrol4 as ic4


PROP_REPORT = [
    ("UserSetSelector", ic4.PropId.USER_SET_SELECTOR),
    ("UserSetDefault", ic4.PropId.USER_SET_DEFAULT),
    ("Width", ic4.PropId.WIDTH),
    ("Height", ic4.PropId.HEIGHT),
    ("WidthMax", ic4.PropId.WIDTH_MAX),
    ("HeightMax", ic4.PropId.HEIGHT_MAX),
    ("SensorWidth", ic4.PropId.SENSOR_WIDTH),
    ("SensorHeight", ic4.PropId.SENSOR_HEIGHT),
    ("OffsetX", ic4.PropId.OFFSET_X),
    ("OffsetY", ic4.PropId.OFFSET_Y),
    ("OffsetAutoCenter", ic4.PropId.OFFSET_AUTO_CENTER),
    ("AcquisitionFrameRate", ic4.PropId.ACQUISITION_FRAME_RATE),
    ("ExposureAuto", ic4.PropId.EXPOSURE_AUTO),
    ("ExposureTime", ic4.PropId.EXPOSURE_TIME),
    ("GainAuto", ic4.PropId.GAIN_AUTO),
    ("Gain", ic4.PropId.GAIN),
]


class QueueListener(ic4.QueueSinkListener):
    def __init__(self, buffer_count=8):
        super().__init__()
        self.buffer_count = buffer_count

    def sink_connected(self, sink, _image_type, min_buffers_required):
        sink.alloc_and_queue_buffers(max(self.buffer_count, min_buffers_required))
        return True

    def frames_queued(self, _sink):
        return


def parse_camera_id(value):
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        return text


def load_setup_from_config(config_path, setup_name=None):
    config = configparser.ConfigParser()
    config.read(config_path)
    section_names = [name for name in config.sections() if "CameraID" in config[name] or "DeviceSerial" in config[name]]
    if not section_names:
        raise ValueError(f"No camera setups found in {config_path}")
    if setup_name is None:
        setup_name = section_names[0]
    if setup_name not in config:
        raise ValueError(f"Setup '{setup_name}' not found in {config_path}")
    section = config[setup_name]
    serial = section.get("DeviceSerial", "").strip()
    cam_id = serial if serial else parse_camera_id(section["CameraID"])
    return setup_name, cam_id


def resolve_device(devices, identifier):
    if isinstance(identifier, int):
        if 1 <= identifier <= len(devices):
            return devices[identifier - 1]
        if 0 <= identifier < len(devices):
            return devices[identifier]
        raise IndexError(f"Camera identifier {identifier} did not resolve to an enumerated device")

    text = str(identifier).strip()
    for device in devices:
        if device.serial == text:
            return device
    raise LookupError(f"Could not resolve device serial '{text}'")


def read_property(prop_map, prop_id):
    getters = (
        ("bool", prop_map.get_value_bool),
        ("int", prop_map.get_value_int),
        ("float", prop_map.get_value_float),
        ("str", prop_map.get_value_str),
    )
    for kind, getter in getters:
        try:
            return kind, getter(prop_id)
        except Exception:
            continue
    return "unavailable", None


def format_value(value):
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return f"{value:.4f}"
    return str(value)


def dump_properties(prop_map):
    print("Current camera properties")
    print("-------------------------")
    for label, prop_id in PROP_REPORT:
        kind, value = read_property(prop_map, prop_id)
        print(f"{label:24s} {format_value(value)} ({kind})")
    print()


def set_optional_controls(prop_map, args):
    if args.disable_auto_exposure:
        prop_map.set_value(ic4.PropId.EXPOSURE_AUTO, False)
    if args.disable_auto_gain:
        prop_map.set_value(ic4.PropId.GAIN_AUTO, False)
    if args.exposure_time is not None:
        prop_map.set_value(ic4.PropId.EXPOSURE_AUTO, False)
        prop_map.set_value(ic4.PropId.EXPOSURE_TIME, float(args.exposure_time))
    if args.gain is not None:
        try:
            prop_map.set_value(ic4.PropId.GAIN_AUTO, False)
        except Exception:
            pass
        prop_map.set_value(ic4.PropId.GAIN, float(args.gain))
    if args.target_fps is not None:
        prop_map.set_value(ic4.PropId.ACQUISITION_FRAME_RATE, float(args.target_fps))


def measure_stream(grabber, duration_s):
    listener = QueueListener(buffer_count=8)
    sink = ic4.QueueSink(
        listener,
        accepted_pixel_formats=[ic4.PixelFormat.Mono8],
        max_output_buffers=1,
    )
    grabber.stream_setup(sink, setup_option=ic4.StreamSetupOption.ACQUISITION_START)
    timestamps = []
    deadline = time.perf_counter() + duration_s
    try:
        while time.perf_counter() < deadline:
            image = sink.try_pop_output_buffer()
            if image is None:
                time.sleep(0.0005)
                continue
            try:
                timestamps.append(time.perf_counter())
            finally:
                image.release()
    finally:
        grabber.stream_stop()

    if len(timestamps) < 2:
        return {
            "frame_count": len(timestamps),
            "observed_fps": 0.0,
            "mean_interval_ms": None,
            "stdev_interval_ms": None,
        }

    intervals = [b - a for a, b in zip(timestamps, timestamps[1:])]
    observed_fps = (len(timestamps) - 1) / (timestamps[-1] - timestamps[0])
    return {
        "frame_count": len(timestamps),
        "observed_fps": observed_fps,
        "mean_interval_ms": statistics.mean(intervals) * 1000.0,
        "stdev_interval_ms": statistics.pstdev(intervals) * 1000.0 if len(intervals) > 1 else 0.0,
    }


def main():
    parser = argparse.ArgumentParser(description="Inspect one Imaging Source camera and measure frame-rate control.")
    parser.add_argument("--config", default="configuration.txt", help="Path to configuration file.")
    parser.add_argument("--setup", default="", help="Setup section name from configuration.txt.")
    parser.add_argument("--serial", default="", help="Override and select a camera by serial number.")
    parser.add_argument("--camera-index", type=int, default=None, help="Override and select a camera by enumerated index.")
    parser.add_argument("--target-fps", type=float, default=None, help="Set AcquisitionFrameRate before measuring.")
    parser.add_argument("--exposure-time", type=float, default=None, help="Set ExposureTime before measuring.")
    parser.add_argument("--gain", type=float, default=None, help="Set Gain before measuring.")
    parser.add_argument("--disable-auto-exposure", action="store_true", help="Force ExposureAuto=False before measuring.")
    parser.add_argument("--disable-auto-gain", action="store_true", help="Force GainAuto=False before measuring.")
    parser.add_argument("--duration", type=float, default=5.0, help="Seconds to measure streaming frame delivery.")
    args = parser.parse_args()

    with ic4.Library.init_context():
        devices = list(ic4.DeviceEnum.devices())
        print("Enumerated devices")
        print("------------------")
        for idx, device in enumerate(devices, start=1):
            print(f"{idx}: {device.model_name} serial={device.serial}")
        print()

        if not devices:
            raise RuntimeError("No Imaging Source devices enumerated.")

        if args.serial:
            identifier = args.serial.strip()
            selection_label = f"serial {identifier}"
        elif args.camera_index is not None:
            identifier = args.camera_index
            selection_label = f"camera index {identifier}"
        else:
            setup_name, identifier = load_setup_from_config(args.config, setup_name=args.setup or None)
            selection_label = f"setup '{setup_name}' ({identifier})"

        device = resolve_device(devices, identifier)
        print(f"Selected device: {selection_label}")
        print(f"Model={device.model_name} Serial={device.serial}")
        print()

        grabber = ic4.Grabber()
        grabber.device_open(device)
        prop_map = grabber.device_property_map

        print("Before applying overrides")
        print("========================")
        dump_properties(prop_map)

        set_optional_controls(prop_map, args)

        print("After applying overrides")
        print("=======================")
        dump_properties(prop_map)

        stats = measure_stream(grabber, args.duration)
        requested_kind, requested_fps = read_property(prop_map, ic4.PropId.ACQUISITION_FRAME_RATE)
        print("Frame-rate assessment")
        print("---------------------")
        print(f"Requested AcquisitionFrameRate: {format_value(requested_fps)} ({requested_kind})")
        print(f"Observed frame count: {stats['frame_count']}")
        print(f"Observed FPS: {stats['observed_fps']:.4f}")
        if stats["mean_interval_ms"] is not None:
            print(f"Mean inter-frame interval: {stats['mean_interval_ms']:.3f} ms")
            print(f"Interval stdev: {stats['stdev_interval_ms']:.3f} ms")
        if isinstance(requested_fps, (int, float)) and requested_fps not in (0, 0.0):
            delta = stats["observed_fps"] - float(requested_fps)
            pct = (delta / float(requested_fps)) * 100.0
            print(f"Observed minus requested: {delta:.4f} FPS ({pct:+.2f}%)")


if __name__ == "__main__":
    main()
