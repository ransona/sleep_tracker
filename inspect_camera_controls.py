import argparse
import configparser
import json
import time

import cv2


CAPTURE_BACKEND_NAME = "DSHOW"
CAPTURE_BACKEND = cv2.CAP_DSHOW

PROPERTY_SPECS = [
    {
        "name": "AutoExposure",
        "prop_id": cv2.CAP_PROP_AUTO_EXPOSURE,
        "samples": [0.0, 0.25, 0.5, 0.75, 1.0],
    },
    {
        "name": "Exposure",
        "prop_id": cv2.CAP_PROP_EXPOSURE,
        "samples": [-13.0, -11.0, -9.0, -7.0, -5.0, -3.0, -1.0, 0.0],
    },
    {
        "name": "Gain",
        "prop_id": cv2.CAP_PROP_GAIN,
        "samples": [0.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 255.0],
    },
]


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
        if "CameraID" not in config[section_name]:
            continue
        entries.append(
            {
                "section": section_name,
                "camera_id": parse_camera_id(config[section_name]["CameraID"]),
            }
        )
    return entries


def open_capture(camera_id):
    cap = cv2.VideoCapture(camera_id, CAPTURE_BACKEND)
    backend_used = CAPTURE_BACKEND_NAME
    if cap.isOpened():
        return cap, backend_used
    cap.release()
    cap = cv2.VideoCapture(camera_id)
    backend_used = "DEFAULT"
    return cap, backend_used


def rounded(value):
    return round(float(value), 4)


def probe_property(cap, spec, settle_ms):
    original = cap.get(spec["prop_id"])
    observations = []
    seen_values = set()

    for requested in spec["samples"]:
        ok = cap.set(spec["prop_id"], requested)
        if settle_ms > 0:
            time.sleep(settle_ms / 1000.0)
        reported = cap.get(spec["prop_id"])
        observed_value = rounded(reported)
        seen_values.add(observed_value)
        observations.append(
            {
                "requested": requested,
                "set_ok": bool(ok),
                "reported": observed_value,
            }
        )

    restore_ok = cap.set(spec["prop_id"], original)
    if settle_ms > 0:
        time.sleep(settle_ms / 1000.0)
    restored = rounded(cap.get(spec["prop_id"]))

    return {
        "original": rounded(original),
        "restore_ok": bool(restore_ok),
        "restored": restored,
        "distinct_reported_values": sorted(seen_values),
        "observations": observations,
    }


def inspect_camera(entry, settle_ms):
    cap, backend_used = open_capture(entry["camera_id"])
    result = {
        "section": entry["section"],
        "camera_id": entry["camera_id"],
        "backend_used": backend_used,
        "opened": bool(cap.isOpened()),
        "properties": {},
    }
    if not cap.isOpened():
        cap.release()
        return result

    result["frame_width"] = rounded(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    result["frame_height"] = rounded(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    result["fps"] = rounded(cap.get(cv2.CAP_PROP_FPS))

    for spec in PROPERTY_SPECS:
        result["properties"][spec["name"]] = probe_property(cap, spec, settle_ms)

    cap.release()
    return result


def print_report(results):
    for result in results:
        header = f"{result['section']} (CameraID={result['camera_id']}, backend={result['backend_used']})"
        print(header)
        print("-" * len(header))
        if not result["opened"]:
            print("  Could not open camera")
            print()
            continue
        print(
            f"  Stream: {result['frame_width']:.0f}x{result['frame_height']:.0f}, "
            f"reported_fps={result['fps']:.4f}"
        )
        for name, prop in result["properties"].items():
            distinct_text = ", ".join(f"{value:.4f}" for value in prop["distinct_reported_values"])
            print(
                f"  {name}: original={prop['original']:.4f}, restored={prop['restored']:.4f}, "
                f"distinct_reported=[{distinct_text}]"
            )
            for observation in prop["observations"]:
                print(
                    f"    request={observation['requested']:.4f} -> "
                    f"ok={observation['set_ok']} reported={observation['reported']:.4f}"
                )
        print()


def main():
    parser = argparse.ArgumentParser(description="Probe camera control values via OpenCV.")
    parser.add_argument(
        "--config",
        default="configuration.txt",
        help="Path to configuration.txt with CameraID entries.",
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=150,
        help="Milliseconds to wait after each property write before reading it back.",
    )
    parser.add_argument(
        "--json-out",
        default="",
        help="Optional path to save the raw probe results as JSON.",
    )
    args = parser.parse_args()

    entries = load_camera_entries(args.config)
    results = [inspect_camera(entry, args.settle_ms) for entry in entries]
    print_report(results)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
