from fastapi import FastAPI, UploadFile, Form, File, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import subprocess, os, uuid, shutil
from typing import List

app = FastAPI()
BASE_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")
# Hỗ trợ truy cập dưới tiền tố /VIDEO/ nếu người dùng mở http://localhost:8000/VIDEO/
app.mount("/VIDEO/static", StaticFiles(directory=STATIC_DIR), name="video_static")
app.mount("/VIDEO/outputs", StaticFiles(directory=OUTPUT_DIR), name="video_outputs")

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

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/VIDEO/")
def index_under_video():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

async def _create_video_multi_impl(request: Request, images: List[UploadFile], script: str):
    # Kiểm tra FFmpeg (tìm nhiều vị trí phổ biến trên Windows)
    ffmpeg_path = _find_ffmpeg_executable()
    if not ffmpeg_path:
        return {"error": "FFmpeg chưa được cài hoặc chưa có trong PATH. Hãy cài bằng winget: winget install --id FFmpeg.FFmpeg -e --source winget"}

    lines = [l.strip() for l in script.splitlines() if l.strip()]
    if not lines:
        return {"error": "Kịch bản trống."}
    if len(lines) != len(images):
        return {"error": f"Số dòng ({len(lines)}) không khớp số ảnh ({len(images)})."}

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
        font_path = next((p for p in possible_fonts if os.path.isfile(p)), None)
    else:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    for i, (img_path, text) in enumerate(zip(img_paths, lines), start=1):
        out_clip = os.path.join(OUTPUT_DIR, f"clip_{i}.mp4")
        safe_text = text.replace("'", r"\'")
        if font_path:
            font_escaped = _escape_path_for_drawtext(font_path)
            text_escaped = _escape_for_drawtext_text(safe_text)
            filter_str = (
                f"drawtext=fontfile='{font_escaped}':text='{text_escaped}':fontcolor=white:fontsize=40:"
                f"x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.5"
            )
        else:
            # Fallback không chỉ định fontfile (ffmpeg tự chọn)
            text_escaped = _escape_for_drawtext_text(safe_text)
            filter_str = (
                f"drawtext=text='{text_escaped}':fontcolor=white:fontsize=40:"
                f"x=(w-text_w)/2:y=(h-text_h)/2:box=1:boxcolor=black@0.5"
            )
        cmd = [
            ffmpeg_path, "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-i", img_path,
            "-vf", filter_str,
            "-t", "5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", out_clip
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

    # Trả về URL công khai, tự động thêm tiền tố /VIDEO nếu người dùng đang dưới /VIDEO/
    base_prefix = "/VIDEO" if str(request.url.path).startswith("/VIDEO/") else ""
    return {"url": f"{base_prefix}/outputs/{final_name}"}


@app.post("/create_video_multi")
async def create_video_multi(request: Request, images: List[UploadFile] = File(...), script: str = Form(...)):
    return await _create_video_multi_impl(request, images, script)


@app.post("/VIDEO/create_video_multi")
async def create_video_multi_under_video(request: Request, images: List[UploadFile] = File(...), script: str = Form(...)):
    return await _create_video_multi_impl(request, images, script)

if __name__ == "__main__":
    import uvicorn
    # Use import string so reload works; main-guard prevents double-run on reload
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
