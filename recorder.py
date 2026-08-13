from __future__ import annotations

import logging
import queue
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from config import RECORDINGS_DIR, ROOT, ensure_storage, settings
from storage import add_segment, init_db


RUNTIME_DIR = ROOT / "runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
RESTART_REQUEST_PATH = RUNTIME_DIR / "recorder.restart"
logging.basicConfig(
    filename=RUNTIME_DIR / "recorder.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
STOP = False
AUTO_DEVICE = "auto"


def stop(*_: object) -> None:
    global STOP
    STOP = True


def open_segment(started_at: datetime) -> tuple[Path, Path, sf.SoundFile]:
    filename = started_at.strftime("%Y-%m-%d_%H-%M-%S.wav")
    final_path = RECORDINGS_DIR / filename
    partial_path = final_path.with_suffix(".part.wav")
    writer = sf.SoundFile(
        partial_path, mode="w", samplerate=settings.sample_rate, channels=settings.channels, subtype="PCM_16"
    )
    logging.info("Recording %s", final_path.name)
    return final_path, partial_path, writer


def finalize(final_path: Path, partial_path: Path, writer: sf.SoundFile, started_at: datetime, frames: int) -> None:
    writer.close()
    if frames == 0:
        partial_path.unlink(missing_ok=True)
        return
    partial_path.replace(final_path)
    add_segment(final_path, started_at, frames / settings.sample_rate)
    logging.info("Saved %s (%.1fs)", final_path.name, frames / settings.sample_rate)


def select_loudest_input_device() -> int:
    """Sample each available input and return the device with the highest RMS level."""
    candidates = [
        (index, device)
        for index, device in enumerate(sd.query_devices())
        if int(device["max_input_channels"]) >= settings.channels
    ]
    if not candidates:
        raise RuntimeError("No audio input devices are available")

    measurements: list[tuple[float, int, str]] = []
    frames = max(1, round(settings.sample_rate * 0.5))
    for index, device in candidates:
        try:
            with sd.InputStream(
                samplerate=settings.sample_rate,
                channels=settings.channels,
                dtype="float32",
                device=index,
            ) as stream:
                samples, overflowed = stream.read(frames)
            if overflowed:
                logging.warning("Input overflow while checking device %s", index)
            rms = float(np.sqrt(np.mean(np.square(samples))))
            measurements.append((rms, index, str(device["name"])))
            logging.info("Input device check: %s (%s), %.1f dBFS", index, device["name"], 20 * np.log10(max(rms, 1e-12)))
        except Exception as exc:
            logging.warning("Cannot check input device %s (%s): %s", index, device["name"], exc)

    if not measurements:
        raise RuntimeError("No audio input device supports the configured recording format")
    level, index, name = max(measurements)
    logging.info("Selected loudest input device: %s (%s), %.1f dBFS", index, name, 20 * np.log10(max(level, 1e-12)))
    return index


def recording_device() -> str | int | None:
    """Use a manually selected device, or detect the loudest available microphone."""
    if settings.device and settings.device.lower() != AUTO_DEVICE:
        return settings.device
    return select_loudest_input_device()


def main() -> None:
    global STOP
    ensure_storage()
    init_db()
    audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
    segment_frames = settings.segment_seconds * settings.sample_rate
    callback_blocks = 0
    written_blocks = 0

    def callback(indata: np.ndarray, frames: int, time: object, status: sd.CallbackFlags) -> None:
        nonlocal callback_blocks
        callback_blocks += 1
        if callback_blocks == 1:
            logging.info("Audio callback started: frames=%s device=%s", frames, settings.device or "default")
        if status:
            logging.warning("Audio status: %s", status)
        try:
            audio_queue.put_nowait(indata.copy())
        except queue.Full:
            # A full queue means the disk writer cannot keep up. Stopping is preferable to silently losing audio.
            logging.critical("Audio queue overflow; stopping recorder to prevent silent data loss")
            raise sd.CallbackAbort

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    final_path: Path | None = None
    partial_path: Path | None = None
    writer: sf.SoundFile | None = None
    started_at: datetime | None = None
    written = 0
    last_flush = time.monotonic()

    try:
        device = recording_device()
        logging.info("Starting recorder with device=%s", device)
        with sd.InputStream(
            samplerate=settings.sample_rate,
            channels=settings.channels,
            dtype="float32",
            device=device,
            callback=callback,
        ) as stream:
            started_at = datetime.now().astimezone()
            final_path, partial_path, writer = open_segment(started_at)
            stream_stopped = False
            while not STOP or not audio_queue.empty():
                if RESTART_REQUEST_PATH.exists():
                    RESTART_REQUEST_PATH.unlink(missing_ok=True)
                    STOP = True
                if STOP and not stream_stopped:
                    # Stop callbacks first, then drain blocks already accepted by the queue.
                    stream.stop()
                    stream_stopped = True
                try:
                    block = audio_queue.get(timeout=0.5)
                except queue.Empty:
                    if STOP:
                        break
                    continue
                offset = 0
                written_blocks += 1
                if written_blocks == 1:
                    logging.info("Audio writer started")
                # Split an arriving callback buffer across segment boundaries exactly; no recording gap.
                while offset < len(block):
                    capacity = segment_frames - written
                    take = min(capacity, len(block) - offset)
                    writer.write(block[offset : offset + take])
                    offset += take
                    written += take
                    if written == segment_frames:
                        finalize(final_path, partial_path, writer, started_at, written)
                        started_at = datetime.now().astimezone()
                        final_path, partial_path, writer = open_segment(started_at)
                        written = 0
                        last_flush = time.monotonic()
                if time.monotonic() - last_flush >= 1:
                    writer.flush()
                    last_flush = time.monotonic()
    except Exception:
        logging.exception("Recorder stopped unexpectedly")
        raise
    finally:
        if writer and final_path and partial_path and started_at:
            finalize(final_path, partial_path, writer, started_at, written)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
