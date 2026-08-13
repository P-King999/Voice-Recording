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
CALLBACK_TIMEOUT_SECONDS = 10
HEARTBEAT_PATH = RUNTIME_DIR / "recorder.heartbeat"
WRITER_HEARTBEAT_PATH = RUNTIME_DIR / "recorder.writer.heartbeat"


class RecoverableAudioError(RuntimeError):
    """An audio stream failed and should be reopened with a fresh device selection."""


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
    """Prefer a usable system default input, then fall back to the loudest input."""
    candidates = [
        (index, device)
        for index, device in enumerate(sd.query_devices())
        if int(device["max_input_channels"]) >= settings.channels
    ]
    if not candidates:
        raise RuntimeError("No audio input devices are available")

    measurements: list[tuple[float, int, str]] = []
    frames = max(1, round(settings.sample_rate * 0.5))
    default_input = sd.default.device[0] if sd.default.device else None
    default_measurement: tuple[float, int, str] | None = None
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
            if index == default_input:
                default_measurement = measurements[-1]
            logging.info("Input device check: %s (%s), %.1f dBFS", index, device["name"], 20 * np.log10(max(rms, 1e-12)))
        except Exception as exc:
            logging.warning("Cannot check input device %s (%s): %s", index, device["name"], exc)

    if not measurements:
        raise RuntimeError("No audio input device supports the configured recording format")
    if default_measurement:
        level, index, name = default_measurement
        logging.info("Selected system default input device: %s (%s)", index, name)
    else:
        level, index, name = max(measurements)
    logging.info("Selected loudest input device: %s (%s), %.1f dBFS", index, name, 20 * np.log10(max(level, 1e-12)))
    return index


def recording_device(force_auto: bool = False) -> str | int | None:
    """Use a manually selected device, or detect the loudest available microphone."""
    if not force_auto and settings.device and settings.device.lower() != AUTO_DEVICE:
        if settings.device.isdigit():
            # Earlier versions saved transient PortAudio indexes. Migrate a stale
            # index to the local Realtek microphone instead of recording silence.
            index = int(settings.device)
            devices = sd.query_devices()
            if 0 <= index < len(devices) and int(devices[index]["max_input_channels"]) >= settings.channels:
                return index
            realtek = next(
                (str(device["name"]) for device in devices if "realtek" in str(device["name"]).lower()
                 and int(device["max_input_channels"]) >= settings.channels),
                None,
            )
            if realtek:
                logging.warning("Configured device index %s is stale; using %s", settings.device, realtek)
                return realtek
        return settings.device
    return select_loudest_input_device()


def record_once(force_auto_device: bool = False) -> None:
    global STOP
    ensure_storage()
    init_db()
    audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
    segment_frames = settings.segment_seconds * settings.sample_rate
    callback_blocks = 0
    written_blocks = 0
    capture_error: str | None = None
    last_callback = time.monotonic()
    last_heartbeat = 0.0

    def callback(indata: np.ndarray, frames: int, callback_time: object, status: sd.CallbackFlags) -> None:
        nonlocal callback_blocks, capture_error, last_callback, last_heartbeat
        callback_blocks += 1
        last_callback = time.monotonic()
        if last_callback - last_heartbeat >= 2:
            HEARTBEAT_PATH.touch()
            last_heartbeat = last_callback
        if callback_blocks == 1:
            logging.info("Audio callback started: frames=%s device=%s", frames, settings.device or "default")
        if status:
            capture_error = f"Audio stream status: {status}"
            logging.error("%s; restarting recorder", capture_error)
            raise sd.CallbackAbort
        try:
            audio_queue.put_nowait(indata.copy())
        except queue.Full:
            # A full queue means the disk writer cannot keep up. Stopping is preferable to silently losing audio.
            capture_error = "Audio queue overflow; writer cannot keep up"
            logging.critical("%s; restarting recorder", capture_error)
            raise sd.CallbackAbort

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    final_path: Path | None = None
    partial_path: Path | None = None
    writer: sf.SoundFile | None = None
    started_at: datetime | None = None
    written = 0

    try:
        device = recording_device(force_auto_device)
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
                    if capture_error:
                        raise RecoverableAudioError(capture_error)
                    if time.monotonic() - last_callback >= CALLBACK_TIMEOUT_SECONDS:
                        raise RecoverableAudioError(
                            f"No audio callback received for {CALLBACK_TIMEOUT_SECONDS} seconds"
                        )
                    continue
                offset = 0
                written_blocks += 1
                if written_blocks == 1:
                    logging.info("Audio writer started")
                WRITER_HEARTBEAT_PATH.touch()
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
    except RecoverableAudioError:
        raise
    except Exception:
        logging.exception("Recorder stopped unexpectedly")
        raise
    finally:
        if writer and final_path and partial_path and started_at:
            finalize(final_path, partial_path, writer, started_at, written)


def main() -> None:
    global STOP
    retry_delay = 2
    force_auto_device = False
    while not STOP:
        try:
            record_once(force_auto_device)
            retry_delay = 2
        except RecoverableAudioError as exc:
            logging.error("Recorder will retry after an audio failure: %s", exc)
        except Exception:
            logging.exception("Recorder stopped unexpectedly; will retry")
            if settings.device and settings.device.lower() != AUTO_DEVICE:
                force_auto_device = True
                logging.warning("Configured input device is unavailable; falling back to automatic device selection")
        if STOP:
            break
        logging.info("Retrying recorder in %s seconds", retry_delay)
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
