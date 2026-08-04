from __future__ import annotations

import logging
import queue
import signal
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from config import RECORDINGS_DIR, ensure_storage, settings
from storage import add_segment, init_db


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
STOP = False


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


def main() -> None:
    global STOP
    ensure_storage()
    init_db()
    audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)
    segment_frames = settings.segment_seconds * settings.sample_rate

    def callback(indata: np.ndarray, frames: int, time: object, status: sd.CallbackFlags) -> None:
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

    try:
        with sd.InputStream(
            samplerate=settings.sample_rate,
            channels=settings.channels,
            dtype="float32",
            device=settings.device,
            callback=callback,
        ):
            started_at = datetime.now().astimezone()
            final_path, partial_path, writer = open_segment(started_at)
            while not STOP:
                try:
                    block = audio_queue.get(timeout=1)
                except queue.Empty:
                    continue
                offset = 0
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
