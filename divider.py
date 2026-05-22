import math
import os
import shutil
import subprocess
import wave
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path


def _probe_wav_params(path: Path) -> tuple[int, int] | None:
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getframerate(), wf.getnchannels()
    except Exception:
        return None


def _probe_ogg_params(path: Path) -> tuple[int, int] | None:
    try:
        blob = path.read_bytes()
    except Exception:
        return None

    if len(blob) < 64 or blob[:4] != b"OggS":
        return None

    # Opus in Ogg container
    head = blob.find(b"OpusHead", 0, min(len(blob), 65536))
    if head != -1 and head + 19 <= len(blob):
        channels = blob[head + 9]
        samplerate = int.from_bytes(blob[head + 12 : head + 16], "little", signed=False)
        if channels > 0 and samplerate > 0:
            return samplerate, channels

    # Vorbis identification header: 0x01 + "vorbis"
    off = blob.find(b"\x01vorbis", 0, min(len(blob), 2_000_000))
    if off == -1 or off + 30 > len(blob):
        return None

    channels = blob[off + 11]
    samplerate = int.from_bytes(blob[off + 12 : off + 16], "little", signed=False)
    if channels > 0 and samplerate > 0:
        return samplerate, channels

    return None


def _probe_flac_params(path: Path) -> tuple[int, int] | None:
    try:
        blob = path.read_bytes()
    except Exception:
        return None

    if len(blob) < 42 or blob[:4] != b"fLaC":
        return None

    try:
        block_header = blob[4:8]
        block_type = block_header[0] & 0x7F
        block_len = int.from_bytes(block_header[1:4], "big", signed=False)
        if block_type != 0 or block_len < 18:
            return None
        streaminfo = blob[8 : 8 + block_len]
        if len(streaminfo) < 18:
            return None

        # sample_rate(20), channels-1(3), bits_per_sample-1(5), total_samples(36)
        x = int.from_bytes(streaminfo[10:18], "big", signed=False)
        samplerate = (x >> 44) & 0xFFFFF
        channels_minus1 = (x >> 41) & 0x7
        channels = int(channels_minus1) + 1
        if channels > 0 and samplerate > 0:
            return int(samplerate), int(channels)
    except Exception:
        return None

    return None


def _probe_mp3_params(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()
    except Exception:
        return None

    if len(data) < 4:
        return None

    # Skip ID3v2 tag if present
    i = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        size = (
            ((data[6] & 0x7F) << 21)
            | ((data[7] & 0x7F) << 14)
            | ((data[8] & 0x7F) << 7)
            | (data[9] & 0x7F)
        )
        i = 10 + size

    # version_id: 00=2.5, 10=2, 11=1 (01 reserved)
    sr_table = {
        0b00: [11025, 12000, 8000],
        0b10: [22050, 24000, 16000],
        0b11: [44100, 48000, 32000],
    }

    # Scan for a frame sync (cap scan window)
    for off in range(i, min(len(data) - 4, i + 256_000)):
        b0 = data[off]
        b1 = data[off + 1]
        if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
            continue

        header = int.from_bytes(data[off : off + 4], "big", signed=False)
        version_id = (header >> 19) & 0b11
        layer = (header >> 17) & 0b11
        sr_index = (header >> 10) & 0b11
        channel_mode = (header >> 6) & 0b11

        if version_id == 0b01:
            continue  # reserved
        if layer == 0b00:
            continue  # reserved
        if sr_index == 0b11:
            continue  # invalid

        sr_list = sr_table.get(version_id)
        if not sr_list:
            continue
        samplerate = sr_list[sr_index]
        channels = 1 if channel_mode == 0b11 else 2
        return int(samplerate), int(channels)

    return None


def probe_input_audio_params(path: Path) -> tuple[int, int] | None:
    """
    Best-effort probe for the *source* audio sample rate + channels.

    If probing fails, return None and the pipeline will fall back to the VLC-decoded WAV params.
    """
    if FFPROBE_PATH is not None:
        try:
            result = subprocess.run(
                [
                    FFPROBE_PATH,
                    '-v',
                    'error',
                    '-select_streams',
                    'a:0',
                    '-show_entries',
                    'stream=sample_rate,channels',
                    '-of',
                    'default=noprint_wrappers=1:nokey=1',
                    str(path),
                ],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            out = (result.stdout or '').strip().splitlines()
            if len(out) >= 2:
                sr = int(out[0].strip())
                ch = int(out[1].strip())
                if sr > 0 and ch > 0:
                    return sr, ch
        except Exception:
            pass


    suf = path.suffix.lower()
    if suf == ".wav":
        return _probe_wav_params(path)
    if suf in (".ogg", ".opus"):
        return _probe_ogg_params(path)
    if suf == ".flac":
        return _probe_flac_params(path)
    if suf == ".mp3":
        return _probe_mp3_params(path)

    return None

SUPPORTED_EXTENSIONS = {
    ".mp3",
    ".ogg",
    ".wav",
    ".flac",
    ".aac",
    ".m4a",
    ".wma",
    ".opus",
    ".aiff",
    ".alac",
    ".mp4",
    ".webm",
    ".mkv",
    ".mov",
}

SCRIPT_DIR = Path(__file__).parent.resolve()


def find_vlc():
    possible_paths = [
        "C:/Program Files/VideoLAN/VLC/vlc.exe",
        "C:/Program Files (x86)/VideoLAN/VLC/vlc.exe",
    ]

    for path in possible_paths:
        if os.path.exists(path):
            return path

    return None


VLC_PATH = find_vlc()

FFMPEG_PATH = shutil.which("ffmpeg")
FFPROBE_PATH = shutil.which("ffprobe")

if FFMPEG_PATH is None and VLC_PATH is None:
    print("\nSem az ffmpeg, sem a VLC nincs el?rhet? (PATH / telep?t?s).\n")
    input("ENTER...")
    raise SystemExit

CODEC_MAP = {
    "ogg": ("vorbis", "ogg"),
    "mp3": ("mp3", "mp3"),
    "wav": ("s16l", "wav"),
    "flac": ("flac", "raw"),
}


def get_media_files():
    return sorted(
        [
            file.name
            for file in SCRIPT_DIR.iterdir()
            if (file.is_file() and file.suffix.lower() in SUPPORTED_EXTENSIONS)
        ]
    )


def print_media_files():
    files = get_media_files()

    if not files:
        print("\nNem találtam audio/video fájlt ebben a mappában.\n")
        return

    print("\nTalált audio/video fájlok:\n")
    for file in files:
        print(f" - {file}")
    print()


def file_exists(filename):
    path = SCRIPT_DIR / filename
    return (
        path.exists()
        and path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def resolve_media_path(filename: str) -> Path | None:
    """
    Windows-on a fájlrendszer case-insensitive, ezért a felhasználó eltérő
    kis/nagybetűkkel is beírhatja a nevet. Itt visszaadjuk a tényleges fájlnevet
    (eredeti case-szel) a mappából.
    """

    wanted = filename.strip().lower()
    for file in SCRIPT_DIR.iterdir():
        if file.is_file() and file.name.lower() == wanted:
            return file

    # fallback (ha pl. abszolút útvonalat adtak meg, vagy valamiért nem listázható)
    candidate = SCRIPT_DIR / filename
    if candidate.exists() and candidate.is_file():
        return candidate

    return None


def get_media_duration_windows(filepath: Path):
    folder_path = str(filepath.parent).replace("'", "''")
    file_name = filepath.name.replace("'", "''")

    powershell_script = f"""
Add-Type -AssemblyName Shell32
$shell = New-Object -ComObject Shell.Application
$folder = $shell.Namespace('{folder_path}')
$file = $folder.ParseName('{file_name}')
$duration = $folder.GetDetailsOf($file, 27)
Write-Output $duration
"""

    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", powershell_script],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    output = result.stdout.strip()
    if not output:
        return None

    parts = output.split(":")

    try:
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + int(s)
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + int(s)
    except ValueError:
        return None

    return None


def get_wav_duration(filepath: Path):
    with wave.open(str(filepath), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        return frames / float(rate)


def format_duration(seconds: float):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h > 0:
        return f"{h:02}:{m:02}:{s:02}"
    return f"{m:02}:{s:02}"


def vlc_transcode_to_wav(input_path: Path, wav_path: Path):
    return vlc_transcode_to_wav_with_params(
        input_path,
        wav_path,
        samplerate=None,
        channels=None,
    )


def vlc_transcode_to_wav_with_params(
    input_path: Path,
    wav_path: Path,
    *,
    samplerate: int | None,
    channels: int | None,
):
    extra = ""
    if samplerate is not None:
        extra += f",samplerate={samplerate}"
    if channels is not None:
        extra += f",channels={channels}"

    wav_sout = (
        f"#transcode{{acodec=s16l{extra}}}:"
        f"std{{access=file,mux=wav,dst='{wav_path}'}}"
    )

    command = [
        VLC_PATH,
        "--intf",
        "dummy",
        "--dummy-quiet",
        "--no-video",
        str(input_path),
        "--sout",
        wav_sout,
        "vlc://quit",
    ]

    subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    return wav_path.exists() and wav_path.stat().st_size > 0

def ffmpeg_decode_to_wav(
    input_path: Path,
    wav_path: Path,
    *,
    samplerate: int | None,
    channels: int | None,
) -> bool:
    if FFMPEG_PATH is None:
        return False

    cmd = [FFMPEG_PATH, '-y', '-hide_banner', '-loglevel', 'error', '-i', str(input_path)]
    if samplerate is not None:
        cmd += ['-ar', str(samplerate)]
    if channels is not None:
        cmd += ['-ac', str(channels)]
    cmd += ['-c:a', 'pcm_s16le', str(wav_path)]

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    return wav_path.exists() and wav_path.stat().st_size > 0



def vlc_transcode_file(
    input_path: Path,
    output_path: Path,
    extension: str,
    *,
    samplerate: int | None = None,
    channels: int | None = None,
):
    extension = extension.lower().replace(".", "")
    codec, mux = CODEC_MAP.get(extension, ("vorbis", "ogg"))

    extra = ""
    if samplerate is not None:
        extra += f",samplerate={samplerate}"
    if channels is not None:
        extra += f",channels={channels}"

    sout = (
        f"#transcode{{acodec={codec}{extra}}}:"
        f"std{{access=file,mux={mux},dst='{output_path}'}}"
    )

    command = [
        VLC_PATH,
        "--intf",
        "dummy",
        "--dummy-quiet",
        "--no-video",
        str(input_path),
        "--sout",
        sout,
        "vlc://quit",
    ]

    subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    return output_path.exists() and output_path.stat().st_size > 0


def ffmpeg_encode_file(
    input_path: Path,
    output_path: Path,
    extension: str,
    *,
    samplerate: int | None,
    channels: int | None,
) -> bool:
    if FFMPEG_PATH is None:
        return False

    ext = extension.lower().replace('.', '')
    cmd = [FFMPEG_PATH, '-y', '-hide_banner', '-loglevel', 'error', '-i', str(input_path)]
    if samplerate is not None:
        cmd += ['-ar', str(samplerate)]
    if channels is not None:
        cmd += ['-ac', str(channels)]

    if ext == 'ogg':
        cmd += ['-c:a', 'libvorbis', '-q:a', '4']
    elif ext == 'mp3':
        cmd += ['-c:a', 'libmp3lame', '-q:a', '4']
    elif ext == 'wav':
        cmd += ['-c:a', 'pcm_s16le']
    elif ext == 'flac':
        cmd += ['-c:a', 'flac']
    else:
        cmd += ['-c:a', 'libvorbis']

    cmd += [str(output_path)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    return output_path.exists() and output_path.stat().st_size > 0


def verify_duration_via_wav_decode(media_path: Path, expected_seconds: float):
    tmp = media_path.with_name(f"_{media_path.stem}__verify.wav")
    tmp.unlink(missing_ok=True)

    ok = ffmpeg_decode_to_wav(media_path, tmp, samplerate=None, channels=None) if FFMPEG_PATH is not None else vlc_transcode_to_wav(media_path, tmp)
    if not ok:
        tmp.unlink(missing_ok=True)
        return False

    try:
        actual = get_wav_duration(tmp)
    finally:
        tmp.unlink(missing_ok=True)

    return actual


def transcode_one_task(args):
    in_wav_s, out_s, ext, rate, ch = args
    in_wav = Path(in_wav_s)
    out_path = Path(out_s)
    ok = ffmpeg_encode_file(in_wav, out_path, ext, samplerate=rate, channels=ch) if FFMPEG_PATH is not None else vlc_transcode_file(in_wav, out_path, ext, samplerate=rate, channels=ch)
    return in_wav_s, out_s, ok


def _parse_samplerate(text: str) -> int | None:
    text = text.strip().lower().replace(",", ".")
    if not text:
        return None

    # allow "44.1" (kHz) or "44100" (Hz) or "44.1k"
    is_k = text.endswith("k")
    if is_k:
        text = text[:-1].strip()

    value = float(text)
    if value <= 0:
        raise ValueError("samplerate must be > 0")

    if is_k or value < 1000:
        value *= 1000.0

    return int(round(value))


def _parse_channels(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    ch = int(text)
    if ch <= 0:
        raise ValueError("channels must be > 0")
    return ch


def split_media(
    input_file: str,
    output_extension: str,
    chunk_length: float,
    *,
    target_samplerate: int | None,
    target_channels: int | None,
):
    input_path = resolve_media_path(input_file) or (SCRIPT_DIR / input_file)
    output_extension = output_extension.lower().replace(".", "")

    output_folder_name = f"{input_path.stem}_{output_extension}"
    output_folder = SCRIPT_DIR / output_folder_name
    output_folder.mkdir(exist_ok=True)

    full_wav_path = output_folder / "_full_decode.wav"
    if full_wav_path.exists():
        full_wav_path.unlink(missing_ok=True)

    if target_samplerate is None or target_channels is None:
        forced = probe_input_audio_params(input_path)
        forced_rate = forced[0] if forced else None
        forced_channels = forced[1] if forced else None
    else:
        forced_rate = target_samplerate
        forced_channels = target_channels

    ok = ffmpeg_decode_to_wav(input_path, full_wav_path, samplerate=forced_rate, channels=forced_channels) if FFMPEG_PATH is not None else vlc_transcode_to_wav_with_params(input_path, full_wav_path, samplerate=forced_rate, channels=forced_channels)

    if not ok:
        print("\nNem sikerült WAV-ba dekódolni a bemeneti fájlt.\n")
        return

    duration = get_wav_duration(full_wav_path)
    total_chunks = math.ceil(duration / chunk_length)

    print(f"\nAudio hossz: {format_duration(duration)}")
    print(f"Összes chunk: {total_chunks}\n")

    with wave.open(str(full_wav_path), "rb") as src_wf:
        framerate = src_wf.getframerate()
        sample_width = src_wf.getsampwidth()
        channels = src_wf.getnchannels()
        total_frames = src_wf.getnframes()
        chunk_frames = int(round(chunk_length * framerate))

        # Gyors OGG mód: egyszer bemérjük az encoder "rövidülését", és ugyanazzal a paddel dolgozunk.
        # 0.1s pontossághoz ez bőven elég, és sokkal gyorsabb, mint chunkonként visszamérni.
        ogg_extra_silence_frames = 0
        if output_extension == "ogg":
            probe_wav = output_folder / "_probe.wav"
            probe_ogg = output_folder / "_probe.ogg"
            probe_wav.unlink(missing_ok=True)
            probe_ogg.unlink(missing_ok=True)

            # első chunk mintájára készítünk egy próbadarabot
            src_wf.setpos(0)
            probe_data = src_wf.readframes(chunk_frames)
            have_frames = len(probe_data) // (sample_width * channels)
            need_frames = max(0, chunk_frames - have_frames)
            if need_frames > 0:
                probe_data += b"\x00" * (need_frames * sample_width * channels)

            with wave.open(str(probe_wav), "wb") as out_wf:
                out_wf.setnchannels(channels)
                out_wf.setsampwidth(sample_width)
                out_wf.setframerate(framerate)
                out_wf.writeframes(probe_data)

            ok = vlc_transcode_file(
                probe_wav,
                probe_ogg,
                "ogg",
                samplerate=framerate,
                channels=channels,
            )
            if ok:
                actual = verify_duration_via_wav_decode(probe_ogg, chunk_length)
                if actual is not False:
                    diff = chunk_length - actual
                    if diff > 0:
                        # kicsit felülbecsüljük, hogy biztosan ne legyen rövidebb
                        ogg_extra_silence_frames = int(math.ceil((diff + 0.02) * framerate))

            probe_wav.unlink(missing_ok=True)
            probe_ogg.unlink(missing_ok=True)

        # 1) Először minden chunkot WAV-ba írunk (ez gyors és pontos)
        temp_wavs: list[Path] = []

        for idx in range(1, total_chunks + 1):
            start_frame = (idx - 1) * chunk_frames
            if start_frame >= total_frames:
                break

            temp_wav = output_folder / f"_{idx}.wav"
            temp_wav.unlink(missing_ok=True)
            temp_wavs.append(temp_wav)

            def write_temp_wav(*, extra_silence_frames: int = 0):
                src_wf.setpos(start_frame)
                data = src_wf.readframes(chunk_frames)

                have_frames = len(data) // (sample_width * channels)
                need_frames = max(0, chunk_frames - have_frames)
                if need_frames > 0:
                    data += b"\x00" * (need_frames * sample_width * channels)

                if extra_silence_frames > 0:
                    data += b"\x00" * (extra_silence_frames * sample_width * channels)

                with wave.open(str(temp_wav), "wb") as out_wf:
                    out_wf.setnchannels(channels)
                    out_wf.setsampwidth(sample_width)
                    out_wf.setframerate(framerate)
                    out_wf.writeframes(data)

            write_temp_wav(
                extra_silence_frames=(ogg_extra_silence_frames if output_extension == "ogg" else 0)
            )

            if output_extension == "wav":
                out_file = output_folder / f"{idx}.wav"
                out_file.unlink(missing_ok=True)
                temp_wav.rename(out_file)
                print(f"Kész: {out_file.name}")

        if output_extension != "wav":
            # 2) WAV -> célformátum párhuzamosan (a VLC indítása a drága rész)
            tasks = []
            for temp_wav in temp_wavs:
                idx = int(temp_wav.stem.replace("_", ""))
                out_file = output_folder / f"{idx}.{output_extension}"
                out_file.unlink(missing_ok=True)
                tasks.append(
                    (str(temp_wav), str(out_file), output_extension, framerate, channels)
                )

            default_workers = max(1, min(8, (os.cpu_count() or 2)))
            try:
                max_workers = int(os.environ.get("VLC_WORKERS", str(default_workers)))
            except ValueError:
                max_workers = default_workers

            max_workers = max(1, max_workers)
            with ProcessPoolExecutor(max_workers=max_workers) as ex:
                futures = [ex.submit(transcode_one_task, t) for t in tasks]
                for fut in as_completed(futures):
                    in_wav_s, out_s, ok = fut.result()
                    in_wav = Path(in_wav_s)
                    out_file = Path(out_s)
                    in_wav.unlink(missing_ok=True)
                    if not ok:
                        out_file.unlink(missing_ok=True)
                        print(f"\nHiba: nem sikerült létrehozni: {out_file.name}\n")
                        return

            # rendezett "Kész" lista (különben össze-vissza érkezik a párhuzamos futásból)
            for idx in range(1, len(tasks) + 1):
                print(f"Kész: {idx}.{output_extension}")

    full_wav_path.unlink(missing_ok=True)
    print(f"\nMinden chunk elkészült!")
    print(f"Hely: {output_folder}\n")


def main():
    while True:
        files = get_media_files()
        if not files:
            print("\nNem találtam audio/video fájlt ebben a mappában.\n")
            return

        print("\nTalált audio/video fájlok:\n")
        for file in files:
            print(f" - {file}")
        print()

        try:
            selected_file = input("Melyik fájlt szeretnéd használni?\n> ").strip()
        except EOFError:
            return
        selected_path = resolve_media_path(selected_file)
        if selected_path is None or not file_exists(selected_path.name):
            print("\nIlyen audio/video fájl nem található!\n")
            continue
        break

    duration = get_media_duration_windows(selected_path)
    if duration is not None:
        print(f"\nAudio hossz: {format_duration(duration)}\n")
    else:
        print("\nNem sikerült lekérni a média hosszát.\n")

    output_extension = (
        input("Milyen formátumba legyen?\n(pl: ogg, wav, mp3)\n> ")
        .strip()
        .lower()
        .replace(".", "")
    )

    while True:
        try:
            sr_text = input(
                "\nMintavételi ráta? (pl: 44100 vagy 44.1 vagy 48k; ENTER = forrás)\n> "
            )
            target_samplerate = _parse_samplerate(sr_text)
            break
        except ValueError:
            print("Érvénytelen ráta!")

    while True:
        try:
            ch_text = input("\nCsatornák száma? (pl: 1 vagy 2; ENTER = forrás)\n> ")
            target_channels = _parse_channels(ch_text)
            break
        except ValueError:
            print("Érvénytelen csatornaszám!")

    while True:
        try:
            chunk_size = float(input("\nHány másodpercenként legyen chunk?\n> ").strip())
            if chunk_size <= 0:
                print("0-nál nagyobb szám kell!")
                continue
            break
        except ValueError:
            print("Érvénytelen szám!")

    split_media(
        selected_path.name,
        output_extension,
        chunk_size,
        target_samplerate=target_samplerate,
        target_channels=target_channels,
    )
    input("\nENTER...")


if __name__ == "__main__":
    main()
