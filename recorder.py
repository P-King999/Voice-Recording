from __future__ import annotations

import logging
import os
import queue
import signal
import struct
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
STOP_REQUEST_PATH = RUNTIME_DIR / "recorder.stop"
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
PARTIAL_SUFFIX = ".part.wav"
# "auto" should not silently adopt a virtual loopback device that returns silence.
VIRTUAL_INPUT_HINTS = (
    "transcreen",
    "sonar",
    "stereo mix",
    "立体声混音",
    "sound mapper",
    "vb-audio",
    "voicemeeter",
    "virtual",
    "cable output",
    "obs",
)
# Among real devices, prefer the built-in array over generic "Mic Device" entries.
PHYSICAL_INPUT_HINTS = ("realtek", "microphone array", "麦克风阵列", "内置麦克风", "built-in microphone")
_WINDOWS_CONSOLE_HANDLER: object | None = None


class RecoverableAudioError(RuntimeError):
    """An audio stream failed and should be reopened with a fresh device selection."""


def stop(*_: object) -> None:
    global STOP
    STOP = True


def install_windows_shutdown_handler() -> None:
    """Ask the recorder to finalize its current file before Windows shuts down."""
    if os.name != "nt":
        return

    import ctypes
    from ctypes import wintypes

    # Ctrl+logoff/shutdown are not exposed by Python's signal module.
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    @handler_type
    def console_handler(event: int) -> bool:
        if event in (0, 2, 5, 6):  # Ctrl+C, close, logoff, shutdown
            stop()
            return True
        return False

    global _WINDOWS_CONSOLE_HANDLER
    _WINDOWS_CONSOLE_HANDLER = console_handler
    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(console_handler, True):
        logging.warning("Could not install the Windows shutdown handler")


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


def touch_heartbeat() -> None:
    """Show the watchdog that the recorder is alive, including while retrying."""
    HEARTBEAT_PATH.touch()


def _wav_data_chunk(stream) -> tuple[int, int]:
    """Return (payload offset, payload bytes) of the WAV data chunk."""
    header = stream.read(12)
    if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    stream.seek(0, os.SEEK_END)
    total = stream.tell()
    offset = 12
    while offset + 8 <= total:
        stream.seek(offset)
        chunk_header = stream.read(8)
        chunk_id = chunk_header[:4]
        chunk_size = struct.unpack("<I", chunk_header[4:])[0]
        payload_offset = offset + 8
        if chunk_id == b"data":
            return payload_offset, max(0, total - payload_offset)
        offset = payload_offset + chunk_size + (chunk_size & 1)
    raise ValueError("no data chunk")


def _repair_partial_file(partial: Path) -> Path | None:
    """Rewrite the header of an interrupted WAV and import it as a real segment.

    Windows terminates pythonw processes at shutdown without giving them a
    chance to close the file, so the audio bytes exist on disk while the header
    still claims zero samples. Rebuilding the sizes makes the file playable.
    """
    with partial.open("r+b") as stream:
        data_offset, data_bytes = _wav_data_chunk(stream)
        frame_bytes = settings.channels * 2
        frames = data_bytes // frame_bytes
        if frames <= 0:
            stream.close()
            partial.unlink(missing_ok=True)
            logging.warning("Dropped empty partial recording %s", partial.name)
            return None
        usable = frames * frame_bytes
        total = data_offset + usable
        stream.seek(4)
        stream.write(struct.pack("<I", total - 8))
        stream.seek(data_offset - 4)
        stream.write(struct.pack("<I", usable))
        stream.truncate(total)
        stream.flush()
        os.fsync(stream.fileno())

    final_path = partial.with_name(partial.name[: -len(PARTIAL_SUFFIX)] + ".wav")
    partial.replace(final_path)
    started_at = datetime.strptime(final_path.name[:19], "%Y-%m-%d_%H-%M-%S").astimezone()
    duration = frames / settings.sample_rate
    add_segment(final_path, started_at, duration)
    logging.info("Recovered interrupted recording %s (%.1fs)", final_path.name, duration)
    return final_path


def repair_partial_recordings(
    directory: Path | None = None, minimum_age_seconds: float = 0.0, max_age_days: float | None = 7
) -> list[Path]:
    """Recover WAV files left behind by a shutdown, crash or forced kill.

    Only recent leftovers are imported automatically, so a first upgrade does
    not queue months of old audio for transcription at once.
    """
    directory = directory or RECORDINGS_DIR
    recovered: list[Path] = []
    if not directory.exists():
        return recovered
    now = time.time()
    skipped = 0
    for partial in sorted(directory.glob(f"*{PARTIAL_SUFFIX}")):
        try:
            age = now - partial.stat().st_mtime
            if minimum_age_seconds and age < minimum_age_seconds:
                continue
            if max_age_days is not None and age > max_age_days * 86400:
                skipped += 1
                continue
            result = _repair_partial_file(partial)
        except Exception:
            logging.exception("Could not recover partial recording %s", partial.name)
            continue
        if result is not None:
            recovered.append(result)
    if skipped:
        logging.warning(
            "Left %s partial recording(s) older than %s day(s) untouched; rename or repair them manually",
            skipped,
            max_age_days,
        )
    return recovered


def _looks_virtual(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in VIRTUAL_INPUT_HINTS)


def select_loudest_input_device() -> int:
    """Prefer the system default input so recording begins without probing every device."""
    candidates = [
        (index, device)
        for index, device in enumerate(sd.query_devices())
        if int(device["max_input_channels"]) >= settings.channels
    ]
    if not candidates:
        raise RuntimeError("No audio input devices are available")

    default_input = sd.default.device[0] if sd.default.device else None
    candidate_by_index = {index: device for index, device in candidates}
    if default_input in candidate_by_index:
        device = candidate_by_index[default_input]
        if not _looks_virtual(str(device["name"])):
            logging.info("Selected system default input device: %s (%s)", default_input, device["name"])
            return int(default_input)
        # A virtual loopback (screen-casting, voice-chat routing) is often the
        # Windows default and records pure silence; fall back to real hardware.
        physical = [index for index, device in candidates if not _looks_virtual(str(device["name"]))]
        preferred = next(
            (
                index
                for index in physical
                if any(hint in str(candidate_by_index[index]["name"]).lower() for hint in PHYSICAL_INPUT_HINTS)
            ),
            None,
        )
        replacement = preferred if preferred is not None else (physical[0] if physical else None)
        if replacement is not None:
            logging.warning(
                "System default input %s (%s) looks virtual; using %s (%s) instead",
                default_input,
                device["name"],
                replacement,
                candidate_by_index[replacement]["name"],
            )
            return int(replacement)
        logging.warning("System default input %s is virtual and no physical input was found", default_input)
        return int(default_input)

    index, device = candidates[0]
    logging.warning("System default input is unavailable; using %s (%s)", index, device["name"])
    return index


def resolve_input_name(name: str) -> int | None:
    """Match a configured device name, tolerating the truncation some APIs apply.

    MME reports at most 31 characters ("...Realtek High Definitio"), so an exact
    name from .env would otherwise never match and every start would fall back.
    """
    wanted = name.strip()
    if not wanted:
        return None
    lowered = wanted.lower()
    devices = sd.query_devices()
    candidates = [
        (index, str(device["name"]))
        for index, device in enumerate(devices)
        if int(device["max_input_channels"]) >= settings.channels
    ]
    for index, device_name in candidates:
        if device_name == wanted:
            return index
    for index, device_name in candidates:
        short = device_name.lower()
        if short.startswith(lowered) or lowered.startswith(short):
            return index
    for index, device_name in candidates:
        if lowered in device_name.lower() or device_name.lower() in lowered:
            return index
    return None


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
            return index if 0 <= index < len(devices) else None
        resolved = resolve_input_name(settings.device)
        if resolved is not None:
            return resolved
        logging.warning("Configured input %r was not found; falling back to automatic selection", settings.device)
    return select_loudest_input_device()


def record_once(force_auto_device: bool = False) -> None:
    global STOP
    ensure_storage()
    init_db()
    audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=1024)
    segment_frames = settings.segment_seconds * settings.sample_rate
    callback_blocks = 0
    written_blocks = 0
    capture_error: str | None = None
    last_callback = time.monotonic()
    # The audio callback must stay allocation-free and I/O-free: touching files or
    # logging there starves PortAudio and shows up as "input overflow" restarts.
    overflow_blocks = 0
    first_frames = 0

    def callback(indata: np.ndarray, frames: int, callback_time: object, status: sd.CallbackFlags) -> None:
        nonlocal callback_blocks, capture_error, last_callback, overflow_blocks, first_frames
        callback_blocks += 1
        last_callback = time.monotonic()
        if callback_blocks == 1:
            first_frames = frames
        if status:
            # Dropped samples are a small gap; restarting loses several seconds and
            # produces a fresh file, so keep recording and report it afterwards.
            overflow_blocks += 1
        try:
            audio_queue.put_nowait(indata.copy())
        except queue.Full:
            # A full queue means the disk writer cannot keep up. Stopping is preferable to silently losing audio.
            capture_error = "Audio queue overflow; writer cannot keep up"
            raise sd.CallbackAbort

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    final_path: Path | None = None
    partial_path: Path | None = None
    writer: sf.SoundFile | None = None
    started_at: datetime | None = None
    written = 0

    try:
        touch_heartbeat()
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
            last_heartbeat = 0.0
            logged_overflow = 0
            while not STOP or not audio_queue.empty():
                if RESTART_REQUEST_PATH.exists() or STOP_REQUEST_PATH.exists():
                    RESTART_REQUEST_PATH.unlink(missing_ok=True)
                    STOP_REQUEST_PATH.unlink(missing_ok=True)
                    STOP = True
                now_monotonic = time.monotonic()
                if now_monotonic - last_heartbeat >= 2:
                    touch_heartbeat()
                    last_heartbeat = now_monotonic
                if overflow_blocks > logged_overflow:
                    logging.warning(
                        "Audio input overflow: %s block(s) dropped so far; keeping the current file open",
                        overflow_blocks,
                    )
                    logged_overflow = overflow_blocks
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
                    logging.info("Audio callback started: frames=%s device=%s", first_frames, settings.device or "default")
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
    install_windows_shutdown_handler()
    STOP_REQUEST_PATH.unlink(missing_ok=True)
    touch_heartbeat()
    try:
        recovered = repair_partial_recordings(RECORDINGS_DIR, max_age_days=settings.recover_partial_days)
    except Exception:
        recovered = []
        logging.exception("Could not check for interrupted recordings")
    if recovered:
        logging.info("Recovered %s interrupted recording(s) from the previous run", len(recovered))
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
        retry_until = time.monotonic() + retry_delay
        while not STOP and time.monotonic() < retry_until:
            if STOP_REQUEST_PATH.exists():
                STOP_REQUEST_PATH.unlink(missing_ok=True)
                STOP = True
                break
            touch_heartbeat()
            time.sleep(min(1, retry_until - time.monotonic()))
        retry_delay = min(retry_delay * 2, 30)


def recover_from_command_line(arguments: list[str]) -> bool:
    """`python recorder.py --recover-partials [days]` repairs leftovers by hand."""
    if "--recover-partials" not in arguments:
        return False
    max_age_days: float | None = None
    position = arguments.index("--recover-partials")
    if position + 1 < len(arguments):
        try:
            max_age_days = float(arguments[position + 1])
        except ValueError:
            max_age_days = None
    init_db()
    recovered = repair_partial_recordings(RECORDINGS_DIR, max_age_days=max_age_days)
    print(f"已恢复 {len(recovered)} 个中断的录音文件。")
    for path in recovered:
        print(f"  {path.name}")
    return True


if __name__ == "__main__":
    if recover_from_command_line(sys.argv[1:]):
        sys.exit(0)
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
