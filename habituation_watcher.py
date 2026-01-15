import argparse
import csv
import getpass
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import List, Set

# Defaults for watcher behavior when not provided by the YAML config.
DEFAULT_CONFIG_PATH = "habituation_watcher.yaml"
DEFAULT_PROCESSED_PATH = "/data/common/habituation/already_processed.txt"
DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_LOG_PATH = "/data/common/habituation/habituation_watcher.log"
DEFAULT_SIMULATE = False

from preprocess_scripts import run_step1_batch


@dataclass
class WatcherConfig:
    experiment_list_path: str
    processed_list_path: str = DEFAULT_PROCESSED_PATH
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    log_path: str = DEFAULT_LOG_PATH
    simulate: bool = DEFAULT_SIMULATE

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


def enqueue_exp(logger: logging.Logger, exp_id: str, simulate: bool) -> None:
    user = getpass.getuser()
    step1_config = {
        "userID": user,
        "expIDs": [exp_id],
        "suite2p_config": "",
        "runs2p": False,
        "rundlc": True,
        "runfitpupil": True,
        "runhabituate": True,
    }
    if simulate:
        logger.info("SIMULATE: Would enqueue exp_id=%s for user=%s with %s", exp_id, user, step1_config)
        return
    logger.info("Enqueuing exp_id=%s for user=%s", exp_id, user)
    run_step1_batch.run_step1_batch(step1_config)


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
                    enqueue_exp(logger, exp_id, cfg.simulate)
                    append_processed(cfg.processed_list_path, exp_id)
                    processed.add(exp_id)
                except Exception as e:
                    logger.exception("Failed to enqueue %s: %s", exp_id, e)
            time.sleep(cfg.poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Stopping watcher (keyboard interrupt)")
            break
        except Exception as e:
            logger.exception("Watcher loop error: %s", e)
            time.sleep(cfg.poll_interval_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Background watcher that enqueues new experiments for preprocessing."
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to watcher YAML config file (default: {DEFAULT_CONFIG_PATH}).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = WatcherConfig.from_file(args.config)
    run_loop(config)
