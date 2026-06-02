import argparse
import csv
import logging
import os
import re
import select
import sys
import time
from dataclasses import dataclass
from typing import List, Set, Tuple

# Defaults for watcher behavior when not provided by the YAML config.
DEFAULT_CONFIG_PATH = "habituation_watcher.yaml"
DEFAULT_PROCESSED_PATH = "/data/common/habituation/already_processed.txt"
DEFAULT_POLL_INTERVAL_SECONDS = 1200
DEFAULT_LOG_PATH = "/data/common/habituation/habituation_watcher.log"
DEFAULT_SIMULATE = False
DEFAULT_REMOTE_REPOSITORY_ROOT = "/data/Remote_Repository"
PIPELINE_USER = "machine-pipeline-access"
FILE_CHECK_NAME = "file_check_habituate.txt"

try:
    from preprocess_pipeline.step1 import run_batch
except ModuleNotFoundError:
    # Support running this script directly when lab_pipeline is a sibling repo.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidate_src_roots = [
        os.path.abspath(os.path.join(script_dir, "..", "lab_pipeline", "src")),
        os.environ.get("LAB_PIPELINE_SRC", ""),
    ]
    for src_root in candidate_src_roots:
        if not src_root:
            continue
        if os.path.isdir(os.path.join(src_root, "preprocess_pipeline")):
            if src_root not in sys.path:
                sys.path.insert(0, src_root)
            break

    try:
        from preprocess_pipeline.step1 import run_batch
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Could not import lab_pipeline. Add its src directory to "
            "PYTHONPATH or set LAB_PIPELINE_SRC."
        ) from exc


@dataclass
class WatcherConfig:
    experiment_list_path: str
    processed_list_path: str = DEFAULT_PROCESSED_PATH
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    log_path: str = DEFAULT_LOG_PATH
    simulate: bool = DEFAULT_SIMULATE
    remote_repository_root: str = DEFAULT_REMOTE_REPOSITORY_ROOT

    @classmethod
    def from_file(cls, path: str) -> "WatcherConfig":
        import yaml  # lazy import to keep top-level deps minimal

        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}

        exp_list = data.get("experiment_list_path")
        if not exp_list:
            raise ValueError("experiment_list_path is required in config file")

        return cls(
            experiment_list_path=exp_list,
            processed_list_path=data.get(
                "processed_list_path", cls.processed_list_path
            ),
            poll_interval_seconds=int(
                data.get("poll_interval_seconds", cls.poll_interval_seconds)
            ),
            log_path=data.get("log_path", cls.log_path),
            simulate=bool(data.get("simulate", cls.simulate)),
            remote_repository_root=data.get(
                "remote_repository_root", cls.remote_repository_root
            ),
        )


def load_processed(path: str) -> Set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r") as f:
        return {line.strip() for line in f if line.strip()}


def append_processed(path: str, exp_id: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(exp_id + "\n")


def read_exp_list(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        return [row[0].strip() for row in reader if row and row[0].strip()]


def setup_logging(log_path: str) -> logging.Logger:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logger = logging.getLogger("habituation_watcher")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    if not logger.handlers:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        fh = logging.FileHandler(log_path)
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger


def interactive_wait_for_next_poll(poll_interval_seconds: int) -> None:
    if poll_interval_seconds <= 0:
        return

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        time.sleep(poll_interval_seconds)
        return

    for remaining in range(poll_interval_seconds, 0, -1):
        minutes, seconds = divmod(remaining, 60)
        sys.stdout.write(
            "\rNext poll in "
            f"{minutes:02d}:{seconds:02d}. Press Enter to poll now.   "
        )
        sys.stdout.flush()

        readable, _, _ = select.select([sys.stdin], [], [], 1.0)
        if readable:
            sys.stdin.readline()
            sys.stdout.write("\rPolling now after manual Enter.                     \n")
            sys.stdout.flush()
            return

    sys.stdout.write("\rPolling now after countdown.                        \n")
    sys.stdout.flush()


def animal_id_from_exp_id(exp_id: str) -> str:
    parts = exp_id.split("_")
    if len(parts) < 3 or not parts[2]:
        raise ValueError(f"Could not derive animal ID from exp_id={exp_id!r}")
    return parts[2]


def exp_root_from_id(exp_id: str, remote_repository_root: str) -> str:
    animal_id = animal_id_from_exp_id(exp_id)
    return os.path.join(remote_repository_root, animal_id, exp_id)


def parse_file_check(path: str) -> Tuple[int, List[Tuple[str, int]]]:
    with open(path, "r") as f:
        lines = [line.strip() for line in f if line.strip()]

    if not lines:
        raise ValueError(f"Empty file check file: {path}")

    match = re.fullmatch(r"Total size:\s*(\d+)", lines[0])
    if not match:
        raise ValueError(f"Invalid total-size header in {path}: {lines[0]!r}")
    total_size = int(match.group(1))

    entries: List[Tuple[str, int]] = []
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) < 2:
            raise ValueError(f"Invalid file-check entry in {path}: {line!r}")
        entries.append((parts[0], int(parts[1])))

    if not entries:
        raise ValueError(f"No file entries found in {path}")

    return total_size, entries


def exp_data_ready(exp_id: str, cfg: WatcherConfig) -> Tuple[bool, str]:
    exp_root = exp_root_from_id(exp_id, cfg.remote_repository_root)
    if not os.path.isdir(exp_root):
        return False, f"experiment folder missing: {exp_root}"

    file_check_path = os.path.join(exp_root, FILE_CHECK_NAME)
    if not os.path.exists(file_check_path):
        return False, f"missing {FILE_CHECK_NAME}"

    try:
        expected_total_size, entries = parse_file_check(file_check_path)
    except Exception as exc:
        return False, f"invalid {FILE_CHECK_NAME}: {exc}"

    observed_total_size = 0
    for filename, expected_size in entries:
        file_path = os.path.join(exp_root, filename)
        if not os.path.exists(file_path):
            return False, f"missing file listed in {FILE_CHECK_NAME}: {filename}"
        observed_size = os.path.getsize(file_path)
        if observed_size != expected_size:
            return False, (
                f"size mismatch for {filename}: expected {expected_size}, "
                f"found {observed_size}"
            )
        observed_total_size += observed_size

    if observed_total_size != expected_total_size:
        return False, (
            f"total size mismatch: expected {expected_total_size}, "
            f"found {observed_total_size}"
        )

    return True, f"all {len(entries)} files match {FILE_CHECK_NAME}"


def enqueue_exp(logger: logging.Logger, exp_id: str, simulate: bool) -> None:
    step1_config = {
        "userID": PIPELINE_USER,
        "expIDs": [exp_id],
        "suite2p_config": "",
        "runs2p": False,
        "rundlc": True,
        "runfitpupil": True,
        "runhabituate": True,
    }
    if simulate:
        logger.info(
            "SIMULATE: Would enqueue exp_id=%s for user=%s with %s",
            exp_id,
            PIPELINE_USER,
            step1_config,
        )
        return
    logger.info("Enqueuing exp_id=%s for user=%s", exp_id, PIPELINE_USER)
    run_batch.run_step1_batch_universal(step1_config)


def run_loop(cfg: WatcherConfig) -> None:
    logger = setup_logging(cfg.log_path)
    logger.info("Starting watcher with config: %s", cfg)

    processed = load_processed(cfg.processed_list_path)
    logger.info("Loaded %d already-processed experiment IDs", len(processed))

    # first poll immediately
    while True:
        try:
            exp_ids = read_exp_list(cfg.experiment_list_path)
            new_ids = [eid for eid in exp_ids if eid not in processed]
            if new_ids:
                logger.info("Found %d new experiment IDs", len(new_ids))
            for exp_id in new_ids:
                try:
                    ready, reason = exp_data_ready(exp_id, cfg)
                    if not ready:
                        logger.info(
                            "Found new experiment ID %s but did not process it yet: %s",
                            exp_id,
                            reason,
                        )
                        continue
                    enqueue_exp(logger, exp_id, cfg.simulate)
                    append_processed(cfg.processed_list_path, exp_id)
                    processed.add(exp_id)
                except Exception as e:
                    logger.exception("Failed to enqueue %s: %s", exp_id, e)
            interactive_wait_for_next_poll(cfg.poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Stopping watcher (keyboard interrupt)")
            break
        except Exception as e:
            logger.exception("Watcher loop error: %s", e)
            interactive_wait_for_next_poll(cfg.poll_interval_seconds)


def parse_args() -> argparse.Namespace:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_config_path = os.path.join(script_dir, DEFAULT_CONFIG_PATH)
    parser = argparse.ArgumentParser(
        description="Background watcher that enqueues new experiments for preprocessing."
    )
    parser.add_argument(
        "--config",
        default=default_config_path,
        help=f"Path to watcher YAML config file (default: {default_config_path}).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = WatcherConfig.from_file(args.config)
    run_loop(config)
