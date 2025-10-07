from fastapi import FastAPI, UploadFile, Form, File, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import subprocess, os, uuid, shutil, tempfile, json, time, sys
import logging
import requests
from typing import List

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger("video-app")

app = FastAPI()
BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")

# Ensure directories exist before mounting
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")
# Hỗ trợ truy cập dưới tiền tố /VIDEO/ nếu người dùng mở http://localhost:8000/VIDEO/
app.mount("/VIDEO/static", StaticFiles(directory=STATIC_DIR), name="video_static")
app.mount("/VIDEO/outputs", StaticFiles(directory=OUTPUT_DIR), name="video_outputs")

# (kept for safety)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)


def _find_ffmpeg_executable() -> str | None:
    # 1) PATH
    path_in_env = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if path_in_env:
        return path_in_env
    # 2) WinGet Links
    local_appdata = os.environ.get("LocalAppData")
    if local_appdata:
        links_candidate = os.path.join(local_appdata, "Microsoft", "WinGet", "Links", "ffmpeg.exe")
        if os.path.isfile(links_candidate):
            return links_candidate
        # 3) WinGet Packages (scan shallow)
        packages_dir = os.path.join(local_appdata, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(packages_dir):
            try:
                for root, _dirs, files in os.walk(packages_dir):
                    if "ffmpeg.exe" in files:
                        return os.path.join(root, "ffmpeg.exe")
            except Exception:
                pass
    # 4) Manual common location
    candidate = os.path.join("C:\\", "ffmpeg", "bin", "ffmpeg.exe")
    if os.path.isfile(candidate):
        return candidate
    return None


def _find_ffprobe_executable() -> str | None:
    # 1) PATH
    path_in_env = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if path_in_env:
        return path_in_env
    # 2) WinGet Links
    local_appdata = os.environ.get("LocalAppData")
    if local_appdata:
        links_candidate = os.path.join(local_appdata, "Microsoft", "WinGet", "Links", "ffprobe.exe")
        if os.path.isfile(links_candidate):
            return links_candidate
        # 3) WinGet Packages (scan shallow)
        packages_dir = os.path.join(local_appdata, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(packages_dir):
            try:
                for root, _dirs, files in os.walk(packages_dir):
                    if "ffprobe.exe" in files:
                        return os.path.join(root, "ffprobe.exe")
            except Exception:
                pass
    # 4) Manual common location
    candidate = os.path.join("C:\\", "ffmpeg", "bin", "ffprobe.exe")
    if os.path.isfile(candidate):
        return candidate
    return None


def _ffprobe_duration_seconds(media_path: str) -> float | None:
    ffprobe_path = _find_ffprobe_executable()
    if not ffprobe_path:
        return None
    try:
        proc = subprocess.run(
            [ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", media_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            return None
        out = proc.stdout.decode(errors='ignore').strip()
        if not out:
            return None
        return float(out)
    except Exception:
        return None


def _cleanup_generated_files(aggressive: bool = True, older_than_seconds: int = 600) -> None:
    now = time.time()
    for base in (UPLOAD_DIR, OUTPUT_DIR):
        try:
            for name in os.listdir(base):
                if name == ".gitignore":
                    continue
                path = os.path.join(base, name)
                try:
                    if aggressive:
                        if os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=True)
                        else:
                            os.remove(path)
                    else:
                        mtime = os.path.getmtime(path)
                        if now - mtime > older_than_seconds:
                            if os.path.isdir(path):
                                shutil.rmtree(path, ignore_errors=True)
                            else:
                                os.remove(path)
                except Exception:
                    # best-effort; skip locked files
                    pass
        except Exception:
            pass


def _write_karaoke_ass(text: str, duration_s: float, target_w: int, target_h: int, font_path: str | None, text_color: str = "white", text_effect: str = "kf_fill") -> str:
    # Build a simple karaoke ASS where words reveal progressively over the full duration.
    play_res_x, play_res_y = target_w, target_h
    # Choose a font name hint; libass matches by name
    font_name = "Arial"
    if font_path:
        try:
            font_name = os.path.splitext(os.path.basename(font_path))[0] or "Arial"
        except Exception:
            font_name = "Arial"
    # Keep subtitle size modest relative to video
    base_font_size = max(22, int(target_h * 0.045))
    # Resolve color
    hex_ass = _css_hex_to_ass_bgr(text_color)
    if hex_ass:
        chosen_ass = hex_ass
    else:
        named = (text_color or "white").lower() if isinstance(text_color, str) else "white"
        named_map = {
            "white": "&H00FFFFFF",
            "yellow": "&H0000FFFF",
            "red": "&H000000FF",
            "black": "&H00000000",
            "pink": "&H00CC66FF",
            "blue": "&H00FF0000",
        }
        chosen_ass = named_map.get(named, "&H00FFFFFF")
    # Make karaoke highlight visible: base white text, highlight = chosen color
    primary = "&H00FFFFFF"
    secondary = chosen_ass
    outline = "&H00000000"      # black
    back = "&H64000000"         # shadow

    total_cs = max(1, int(duration_s * 100))
    try:
        logger.info(f"subtitle effect={text_effect} color={text_color} font={font_name}")
    except Exception:
        pass
    words = [w for w in text.split() if w] or [text]
    dialogue_prefix = ""  # extra ASS tags before the text
    if text_effect == "k_word":
        # per-word karaoke
        lengths = [max(1, len(w)) for w in words]
        total_len = sum(lengths)
        k_values = [max(1, int(total_cs * L / total_len)) for L in lengths]
        drift = total_cs - sum(k_values)
        if drift != 0:
            k_values[-1] = max(1, k_values[-1] + drift)
        karaoke_segments = [f"{{\\k{k}}}{w}" for w, k in zip(words, k_values)]
        karaoke_line = " ".join(karaoke_segments)
    elif text_effect == "typewriter":
        # reveal characters progressively using \k per char
        chars = list(text)
        lengths = [1 for _ in chars]
        k_each = max(1, int(total_cs / max(1, len(chars))))
        karaoke_segments = [f"{{\\k{k_each}}}{c}" for c in chars]
        karaoke_line = "".join(karaoke_segments)
    elif text_effect == "fade_in":
        # simple fade in on the whole line
        karaoke_line = text
        dialogue_prefix = "{\\alpha&HFF&\\t(0,700,\\alpha&H00&)}"
    elif text_effect == "pop":
        # pop scale: 0 -> 130% -> 100%
        karaoke_line = text
        dialogue_prefix = "{\\fscx0\\fscy0\\t(0,300,\\fscx130\\fscy130)\\t(300,600,\\fscx100\\fscy100)}"
    else:
        # default smooth fill with \kf
        karaoke_line = f"{{\\kf{total_cs}}}{text}"

    end_s = max(0.01, duration_s)
    def _fmt(t: float) -> str:
        cs = int(round((t - int(t)) * 100))
        s = int(t) % 60
        m = (int(t) // 60) % 60
        h = int(t) // 3600
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font_name},{base_font_size},{primary},{secondary},{outline},{back},0,0,0,0,100,100,0,0,1,3,2,2,30,30,{max(20, int(target_h*0.08))},0",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 0,{_fmt(0.0)},{_fmt(end_s)},Default,,0,0,0,,{{\\bord3\\shad2}}{dialogue_prefix}{karaoke_line}",
        "",
    ]

    fd, ass_path = tempfile.mkstemp(suffix=".ass", dir=OUTPUT_DIR)
    os.close(fd)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(header))
    return ass_path


def _escape_for_drawtext_text(s: str) -> str:
    # Escape characters that break drawtext parsing
    # Reference: ffmpeg drawtext docs (escape \\ : ' %)
    escaped = s.replace('\\', r'\\')
    escaped = escaped.replace(':', r'\:')
    escaped = escaped.replace("'", r"\'")
    escaped = escaped.replace('%', r'\%')
    return escaped


def _escape_path_for_drawtext(path: str) -> str:
    # Use forward slashes and escape drive colon for Windows
    p = path.replace('\\', '/')
    if len(p) >= 2 and p[1] == ':':
        p = p[0] + r'\:' + p[2:]
    return p


def _normalize_drawtext_color(c: str) -> str:
    if not isinstance(c, str):
        return "white"
    c = c.strip()
    if c.startswith('#') and len(c) in (4, 7):
        # Expand short #RGB to #RRGGBB
        if len(c) == 4:
            r, g, b = c[1], c[2], c[3]
            c = f"#{r}{r}{g}{g}{b}{b}"
        # drawtext expects 0xRRGGBB
        return "0x" + c[1:]
    return c


def _css_hex_to_ass_bgr(color: str) -> str | None:
    if not isinstance(color, str):
        return None
    s = color.strip()
    if s.startswith('#') and len(s) in (4, 7):
        if len(s) == 4:
            r, g, b = s[1], s[2], s[3]
            s = f"#{r}{r}{g}{g}{b}{b}"
        try:
            r = int(s[1:3], 16)
            g = int(s[3:5], 16)
            b = int(s[5:7], 16)
            # ASS is &HAABBGGRR (AA alpha first). Use 00 alpha (opaque)
            return f"&H00{b:02X}{g:02X}{r:02X}"
        except Exception:
            return None
    return None


def _synthesize_tts_mp3(text: str, voice: str) -> str | None:
    """Call a local openai-edge-tts compatible server to synthesize MP3.
    Returns path to a temporary mp3 file, or None on failure.
    """
    # Default to 5050 per openai-edge-tts config unless TTS_BASE_URL is set
    base_url = os.environ.get("TTS_BASE_URL", "http://127.0.0.1:5050")
    url = f"{base_url.rstrip('/')}/v1/audio/speech"
    payload = {
        "model": "gpt-4o-mini-tts",
        "voice": voice,
        "input": text,
        "format": "mp3",
    }
    try:
        api_key = os.environ.get("TTS_API_KEY", "local")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
        if resp.status_code != 200:
            return None
        mp3_bytes = resp.content
        fd, temp_path = tempfile.mkstemp(suffix=".mp3", dir=OUTPUT_DIR)
        os.close(fd)
        with open(temp_path, "wb") as f:
            f.write(mp3_bytes)
        return temp_path
    except Exception:
        return None


# ---------- Auto-start local TTS server (openai-edge-tts) ----------
_tts_proc: subprocess.Popen | None = None


def _is_tts_alive(base_url: str) -> bool:
    try:
        # Check Swagger docs which responds to GET with 200
        r = requests.get(base_url.rstrip('/') + "/docs", timeout=1)
        return r.status_code < 500
    except Exception:
        return False


def _ensure_tts_server_running() -> None:
    global _tts_proc
    if os.environ.get("DISABLE_TTS_AUTOSTART", "0") in ("1", "true", "TRUE"):
        return

    base_url = os.environ.get("TTS_BASE_URL", "http://127.0.0.1:5050").rstrip("/")
    if _is_tts_alive(base_url):
        logger.info(f"TTS already running at {base_url}")
        return

    candidate_dir = os.path.join(BASE_DIR, "openai-edge-tts")
    entry_alt = os.path.join(candidate_dir, "app", "server.py")
    if os.path.isfile(entry_alt):
        cmd = [sys.executable, "app\\server.py"]
        cwd = candidate_dir
    else:
        logger.warning("openai-edge-tts not found; skipping TTS autostart")
        return

    try:
        env = os.environ.copy()
        # Ensure API key and correct port from base_url
        env.setdefault("API_KEY", env.get("TTS_API_KEY", "local"))
        env.setdefault("PORT", base_url.rsplit(":", 1)[-1])
        logger.info(f"Starting TTS server: {' '.join(cmd)} (PORT={env['PORT']})")
        _tts_proc = subprocess.Popen(cmd, cwd=cwd, env=env)
        # Wait up to ~20s for server to be ready
        for _ in range(200):
            if _is_tts_alive(base_url):
                logger.info(f"TTS is up at {base_url}")
                break
            time.sleep(0.1)
    except Exception:
        _tts_proc = None


@app.on_event("startup")
def _on_startup() -> None:
    _ensure_tts_server_running()


@app.on_event("shutdown")
def _on_shutdown() -> None:
    global _tts_proc
    if _tts_proc and _tts_proc.poll() is None:
        try:
            _tts_proc.terminate()
        except Exception:
            pass

@app.get("/")
def index():
    # Khi người dùng tải lại trang, dọn rác để tiết kiệm dung lượng
    _cleanup_generated_files(aggressive=True)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/VIDEO/")
def index_under_video():
    _cleanup_generated_files(aggressive=True)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/health")
def health():
    tts_url = os.environ.get("TTS_BASE_URL", "http://127.0.0.1:5050")
    alive = _is_tts_alive(tts_url)
    return JSONResponse({
        "web": "ok",
        "tts_base_url": tts_url,
        "tts_alive": alive
    })

def _color_filter_from_preset(preset: str) -> str:
    p = (preset or "").lower()
    if p == "warm":
        return "eq=contrast=1.05:saturation=1.15:gamma_r=1.08"
    if p == "cool":
        return "eq=contrast=1.05:saturation=1.05:gamma_b=1.10"
    if p == "cinematic":
        # Use a safe combo: slight contrast + reduced saturation + vignette
        return "eq=contrast=1.06:saturation=0.90,vignette=PI/6"
    if p == "bw" or p == "mono":
        return "hue=s=0"
    return ""


async def _create_video_multi_impl(request: Request, images: List[UploadFile], script: str, use_tts: bool = False, tts_voice: str = "en-US-JennyNeural", aspect: str = "16:9", color_grade: str = "", preview: bool = False, bgm: UploadFile | None = None, text_color: str = "white", font_name: str = "auto", text_effect: str = "kf_fill"):
    # Kiểm tra FFmpeg (tìm nhiều vị trí phổ biến trên Windows)
    ffmpeg_path = _find_ffmpeg_executable()
    if not ffmpeg_path:
        return {"error": "FFmpeg chưa được cài hoặc chưa có trong PATH. Hãy cài bằng winget: winget install --id FFmpeg.FFmpeg -e --source winget"}

    # Kịch bản: nếu trống, tự sinh dựa trên tên file; nếu thiếu, tự bù
    raw_lines = [l.rstrip() for l in (script or "").splitlines()]
    lines = [l.strip() for l in raw_lines if l.strip()]
    if not lines:
        # Tạo kịch bản mặc định từ tên file
        lines = []
        for idx, img in enumerate(images, start=1):
            name = os.path.splitext(os.path.basename(img.filename or f"ảnh_{idx}.png"))[0]
            lines.append(name.replace("_", " ") or f"Cảnh {idx}")
    if len(lines) < len(images):
        # Bổ sung phần còn thiếu
        for idx in range(len(lines)+1, len(images)+1):
            lines.append(f"Cảnh {idx}")
    elif len(lines) > len(images):
        lines = lines[:len(images)]

    # Lưu từng ảnh
    img_paths = []
    for img in images:
        path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{img.filename}")
        with open(path, "wb") as f:
            f.write(await img.read())
        img_paths.append(path)

    clip_paths = []

    # Tạo video cho từng ảnh + dòng chữ tương ứng
    if os.name == "nt":
        # Tìm font phổ biến trên Windows
        possible_fonts = [
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/tahoma.ttf",
        ]
        if font_name and font_name != "auto":
            candidate = f"C:/Windows/Fonts/{font_name}"
            font_path = candidate if os.path.isfile(candidate) else next((p for p in possible_fonts if os.path.isfile(p)), None)
        else:
            font_path = next((p for p in possible_fonts if os.path.isfile(p)), None)
    else:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    # Xác định kích thước theo tỉ lệ khung hình
    aspect = (aspect or "16:9").strip()
    if aspect == "9:16":
        target_w, target_h = (608, 1080) if preview else (1080, 1920)
    elif aspect == "1:1":
        target_w, target_h = (720, 720) if preview else (1080, 1080)
    else:
        target_w, target_h = (1280, 720) if preview else (1920, 1080)  # 16:9

    # Thời lượng mặc định mỗi ảnh
    default_duration = 3.0 if preview else 5.0
    fade_dur = 0.6 if default_duration >= 1.2 else max(0.2, default_duration / 4)
    color_filter = _color_filter_from_preset(color_grade)

    # Nếu có nhạc nền, lưu tạm (sẽ trộn sau khi nối video để nhạc chạy liền mạch)
    bgm_path: str | None = None
    if bgm is not None:
        try:
            bgm_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}_{bgm.filename or 'bgm.mp3'}")
            with open(bgm_path, "wb") as f:
                f.write(await bgm.read())
        except Exception:
            bgm_path = None

    for i, (img_path, text) in enumerate(zip(img_paths, lines), start=1):
        out_clip = os.path.join(OUTPUT_DIR, f"clip_{i}.mp4")
        safe_text = text.replace("'", r"\'")
        if font_path:
            font_escaped = _escape_path_for_drawtext(font_path)
            text_escaped = _escape_for_drawtext_text(safe_text)
            # Subtitle near bottom, dynamic size, with border + shadow for readability
            filter_str = (
                f"drawtext=fontfile='{font_escaped}':text='{text_escaped}':"
                f"fontcolor={_normalize_drawtext_color(text_color)}:borderw=2:bordercolor=black@0.9:shadowcolor=black@0.6:shadowx=2:shadowy=2:"
                f"fontsize=h*0.045:x=(w-text_w)/2:y=h*0.88-text_h/2"
            )
        else:
            # Fallback không chỉ định fontfile (ffmpeg tự chọn)
            text_escaped = _escape_for_drawtext_text(safe_text)
            filter_str = (
                f"drawtext=text='{text_escaped}':"
                f"fontcolor={_normalize_drawtext_color(text_color)}:borderw=2:bordercolor=black@0.9:shadowcolor=black@0.6:shadowx=2:shadowy=2:"
                f"fontsize=h*0.045:x=(w-text_w)/2:y=h*0.88-text_h/2"
            )
        # Scale để FIT (không crop nhiều) rồi pad để đủ khung
        scale_filter = (
            f"scale=w={target_w}:h={target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
        # Zoom nhẹ (Ken Burns) và fade mượt
        # Số frame theo fps 30
        duration_s = default_duration
        audio_path = None
        if use_tts and text:
            audio_path = _synthesize_tts_mp3(text, tts_voice)
            if not audio_path:
                return {"error": f"TTS không hoạt động. Kiểm tra server TTS tại {os.environ.get('TTS_BASE_URL', 'http://127.0.0.1:5000')}/v1/audio/speech hoặc đặt TTS_BASE_URL cho đúng."}
            # Build karaoke ASS and overlay via subtitles; rely on -shortest to match audio
            duration_s = _ffprobe_duration_seconds(audio_path) or default_duration
            ass_path = _write_karaoke_ass(text, duration_s, target_w, target_h, font_path, text_color, text_effect)
            ass_escaped = _escape_path_for_drawtext(ass_path)
            frames = max(1, int(duration_s * 30))
            zoom = f"zoompan=z='min(zoom+0.0015,1.06)':d={frames}:s={target_w}x{target_h}:fps=30"
            fades = f"fade=t=in:st=0:d={fade_dur},fade=t=out:st={max(0.0, duration_s-fade_dur)}:d={fade_dur}"
            parts = [scale_filter]
            if color_filter:
                parts.append(color_filter)
            parts.append(zoom)
            parts.append(f"subtitles='{ass_escaped}'")
            parts.append(fades)
            vf_chain = ",".join(parts)
            cmd = [
                ffmpeg_path, "-hide_banner", "-loglevel", "error",
                "-loop", "1", "-i", img_path,
                "-i", audio_path,
                "-vf", vf_chain,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", "-y", out_clip
            ]
        else:
            frames = max(1, int(duration_s * 30))
            zoom = f"zoompan=z='min(zoom+0.0015,1.06)':d={frames}:s={target_w}x{target_h}:fps=30"
            fades = f"fade=t=in:st=0:d={fade_dur},fade=t=out:st={max(0.0, duration_s-fade_dur)}:d={fade_dur}"
            parts = [scale_filter]
            if color_filter:
                parts.append(color_filter)
            parts.append(zoom)
            # Use ASS even without TTS so text animates per selected effect
            ass_path_no_tts = _write_karaoke_ass(text, duration_s, target_w, target_h, font_path, text_color, text_effect)
            ass_escaped_no_tts = _escape_path_for_drawtext(ass_path_no_tts)
            parts.append(f"subtitles='{ass_escaped_no_tts}'")
            parts.append(fades)
            vf_chain = ",".join(parts)
            # Add silent track so concat stays consistent
            cmd = [
                ffmpeg_path, "-hide_banner", "-loglevel", "error",
                "-loop", "1", "-i", img_path,
                "-f", "lavfi", "-t", str(duration_s), "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-vf", vf_chain,
                "-map", "0:v", "-map", "1:a",
                "-t", str(duration_s),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-y", out_clip
            ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            return {"error": f"FFmpeg tạo clip lỗi (ảnh {i}): {proc.stderr.decode(errors='ignore')}"}
        clip_paths.append(out_clip)

    # Tạo file list để nối video
    list_file = os.path.join(OUTPUT_DIR, "list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for clip in clip_paths:
            f.write(f"file '{os.path.abspath(clip)}'\n")

    final_name = f"{uuid.uuid4().hex}.mp4"
    final_path = os.path.join(OUTPUT_DIR, final_name)

    # Nối video
    proc_concat = subprocess.run([
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy", "-y", final_path
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc_concat.returncode != 0:
        return {"error": f"FFmpeg nối video lỗi: {proc_concat.stderr.decode(errors='ignore')}"}

    # Nếu có nhạc nền: trộn nhạc sau cùng để nhạc chạy liền mạch toàn bộ video
    if bgm_path and os.path.isfile(final_path):
        final_with_bgm = os.path.join(OUTPUT_DIR, f"{uuid.uuid4().hex}.mp4")
        proc_mix = subprocess.run([
            ffmpeg_path, "-hide_banner", "-loglevel", "error",
            "-i", final_path,
            "-stream_loop", "-1", "-i", bgm_path,
            "-filter_complex", "[1:a]volume=0.10[music];[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[mix]",
            "-map", "0:v", "-map", "[mix]",
            "-c:v", "copy", "-c:a", "aac", "-shortest", "-y", final_with_bgm
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc_mix.returncode == 0:
            try:
                os.replace(final_with_bgm, final_path)
            except Exception:
                final_path = final_with_bgm

    # Trả về URL công khai, tự động thêm tiền tố /VIDEO nếu người dùng đang dưới /VIDEO/
    base_prefix = "/VIDEO" if str(request.url.path).startswith("/VIDEO/") else ""
    return {"url": f"{base_prefix}/outputs/{final_name}"}


@app.post("/create_video_multi")
async def create_video_multi(request: Request, images: List[UploadFile] = File(...), script: str = Form("") , use_tts: bool = Form(False), tts_voice: str = Form("vi-VN-HoaiMyNeural"), aspect: str = Form("16:9"), color_grade: str = Form("") , bgm: UploadFile | None = File(None), text_color: str = Form("white"), font_name: str = Form("auto"), text_effect: str = Form("kf_fill")):
    return await _create_video_multi_impl(request, images, script, use_tts, tts_voice, aspect, color_grade, preview=False, bgm=bgm, text_color=text_color, font_name=font_name, text_effect=text_effect)


@app.post("/VIDEO/create_video_multi")
async def create_video_multi_under_video(request: Request, images: List[UploadFile] = File(...), script: str = Form("") , use_tts: bool = Form(False), tts_voice: str = Form("vi-VN-HoaiMyNeural"), aspect: str = Form("16:9"), color_grade: str = Form("") , bgm: UploadFile | None = File(None), text_color: str = Form("white"), font_name: str = Form("auto"), text_effect: str = Form("kf_fill")):
    return await _create_video_multi_impl(request, images, script, use_tts, tts_voice, aspect, color_grade, preview=False, bgm=bgm, text_color=text_color, font_name=font_name, text_effect=text_effect)


@app.post("/preview_video_multi")
async def preview_video_multi(request: Request, images: List[UploadFile] = File(...), script: str = Form("") , use_tts: bool = Form(False), tts_voice: str = Form("vi-VN-HoaiMyNeural"), aspect: str = Form("16:9"), color_grade: str = Form("") , bgm: UploadFile | None = File(None), text_color: str = Form("white"), font_name: str = Form("auto"), text_effect: str = Form("kf_fill")):
    return await _create_video_multi_impl(request, images, script, use_tts, tts_voice, aspect, color_grade, preview=True, bgm=bgm, text_color=text_color, font_name=font_name, text_effect=text_effect)


@app.post("/VIDEO/preview_video_multi")
async def preview_video_multi_under_video(request: Request, images: List[UploadFile] = File(...), script: str = Form("") , use_tts: bool = Form(False), tts_voice: str = Form("vi-VN-HoaiMyNeural"), aspect: str = Form("16:9"), color_grade: str = Form("") , bgm: UploadFile | None = File(None), text_color: str = Form("white"), font_name: str = Form("auto"), text_effect: str = Form("kf_fill")):
    return await _create_video_multi_impl(request, images, script, use_tts, tts_voice, aspect, color_grade, preview=True, bgm=bgm, text_color=text_color, font_name=font_name, text_effect=text_effect)

if __name__ == "__main__":
    import uvicorn
    # Use import string so reload works; main-guard prevents double-run on reload
    uvicorn.run("app:app", host="127.0.0.1", port=8080, reload=True)
