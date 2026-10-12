import asyncio
import glob as globmod
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

load_dotenv()

log = logging.getLogger("vtx")
logging.basicConfig(level=logging.INFO)

BASE_URL = os.getenv("BASE_URL", "https://interview-studio-lime.vercel.app")
TRANSFORM_SERVICE_TOKEN = os.getenv("TRANSFORM_SERVICE_TOKEN", "")
MAX_CLIP_BYTES = int(os.getenv("MAX_CLIP_BYTES", "3000000"))
MAX_CLIP_SECONDS = float(os.getenv("MAX_CLIP_SECONDS", "10.0"))

ERROR_MAP = {
    400: "Invalid request. Please attach a valid video clip.",
    401: "Service authentication failed. Check your token.",
    403: "Session is not live. Please try again later.",
    413: f"Each segment must be under {MAX_CLIP_BYTES // 1_000_000} MB.",
    422: "Unsupported format. Only valid MP4 videos are accepted.",
    429: "Too many requests. Please wait and retry.",
    503: "Service temporarily unavailable. Please retry shortly.",
}

RETRYABLE_CODES = {429, 503}
MAX_RETRIES = 2
RETRY_DELAY = 1.0

http_client: httpx.AsyncClient = None  # type: ignore[assignment]


def _get_ffmpeg() -> str:
    """Return the ffmpeg binary path from imageio-ffmpeg or system PATH."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        path = shutil.which("ffmpeg")
        if path:
            return path
    raise HTTPException(status_code=500, detail="FFmpeg is not installed on the server.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    yield
    await http_client.aclose()


app = FastAPI(title="Video Transform API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)")


def _get_duration(filepath: str) -> float:
    """Get video duration in seconds using ffmpeg -i (parses stderr)."""
    ffmpeg = _get_ffmpeg()
    result = subprocess.run(
        [ffmpeg, "-i", filepath],
        capture_output=True, text=True,
    )
    # ffmpeg -i always exits non-zero when no output is specified, but
    # it prints media info to stderr regardless.
    m = _DURATION_RE.search(result.stderr)
    if not m:
        raise HTTPException(status_code=422, detail=ERROR_MAP[422])
    h, mi, s, cs = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return h * 3600 + mi * 60 + s + cs / 100.0


def _split_video(filepath: str, workdir: str, max_bytes: int) -> list[str]:
    """Split a video into segments each strictly < MAX_CLIP_SECONDS and <= max_bytes.

    Uses individual ffmpeg calls with -ss/-t for precise time-based cuts,
    re-encoding each segment so keyframes align perfectly at boundaries.
    """
    ffmpeg = _get_ffmpeg()
    file_size = os.path.getsize(filepath)
    duration = _get_duration(filepath)

    if duration <= 0:
        raise HTTPException(status_code=422, detail=ERROR_MAP[422])

    # Hard cap: each segment must be strictly under 10 seconds
    max_seg_seconds = MAX_CLIP_SECONDS - 0.5  # 9.5s hard cap

    # Also respect size: estimate a safe duration from average bitrate
    avg_byterate = file_size / duration
    size_based_duration = (max_bytes * 0.75) / avg_byterate

    # Use the smaller of the two limits
    segment_duration = max(1.0, min(size_based_duration, max_seg_seconds))

    # If the file already fits both limits, return as-is
    if file_size <= max_bytes and duration < MAX_CLIP_SECONDS:
        log.info(f"File fits limits ({file_size}B, {duration:.2f}s), no split needed")
        return [filepath]

    # Calculate segment count and extract each one individually
    num_segments = math.ceil(duration / segment_duration)
    log.info(
        f"Splitting {duration:.2f}s / {file_size}B into {num_segments} segments "
        f"(target {segment_duration:.2f}s each, max {MAX_CLIP_SECONDS}s / {max_bytes}B)"
    )

    segments: list[str] = []
    for i in range(num_segments):
        start_time = i * segment_duration
        seg_path = os.path.join(workdir, f"seg_{i:03d}.mp4")

        cmd = [
            ffmpeg, "-y",
            "-ss", f"{start_time:.3f}",
            "-i", filepath,
            "-t", f"{segment_duration:.3f}",
            "-c:v", "libx264", "-preset", "fast",
            "-an",
            "-reset_timestamps", "1",
            seg_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"FFmpeg segment {i} failed: {result.stderr[:200]}",
            )

        # Skip empty segments (can happen at the tail)
        if not os.path.exists(seg_path) or os.path.getsize(seg_path) == 0:
            continue

        # Validate this segment
        seg_size = os.path.getsize(seg_path)
        seg_dur = _get_duration(seg_path)
        log.info(f"  Segment {i}: {seg_dur:.2f}s, {seg_size}B")

        if seg_dur > MAX_CLIP_SECONDS or seg_size > max_bytes:
            # Re-split this oversized segment with even smaller chunks
            sub_segs = _resplit_segment(seg_path, workdir, max_bytes)
            segments.extend(sub_segs)
        else:
            segments.append(seg_path)

    if not segments:
        raise HTTPException(status_code=500, detail="FFmpeg produced no valid segments.")

    return segments


def _resplit_segment(filepath: str, workdir: str, max_bytes: int) -> list[str]:
    """Re-encode an oversized segment into smaller chunks that each fit
    under both the byte limit and the 10-second duration cap."""
    ffmpeg = _get_ffmpeg()
    duration = _get_duration(filepath)
    if duration <= 0:
        raise HTTPException(status_code=422, detail="Segment has zero duration.")

    max_seg_seconds = MAX_CLIP_SECONDS - 0.5
    # Size-based limit
    file_size = os.path.getsize(filepath)
    avg_byterate = file_size / duration
    size_based_duration = (max_bytes * 0.60) / avg_byterate  # aggressive headroom

    segment_duration = max(1.0, min(size_based_duration, max_seg_seconds))
    target_bitrate = int((max_bytes * 0.60 * 8) / segment_duration)

    num_segments = math.ceil(duration / segment_duration)
    tag = uuid.uuid4().hex[:8]

    segments: list[str] = []
    for i in range(num_segments):
        start_time = i * segment_duration
        seg_path = os.path.join(workdir, f"reseg_{tag}_{i:03d}.mp4")

        cmd = [
            ffmpeg, "-y",
            "-ss", f"{start_time:.3f}",
            "-i", filepath,
            "-t", f"{segment_duration:.3f}",
            "-c:v", "libx264", "-b:v", str(target_bitrate),
            "-an",
            "-reset_timestamps", "1",
            seg_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"FFmpeg re-split failed: {result.stderr[:200]}",
            )
        if os.path.exists(seg_path) and os.path.getsize(seg_path) > 0:
            segments.append(seg_path)

    return segments


def _concat_videos(segment_paths: list[str], output_path: str) -> None:
    """Concatenate transformed segments back into one MP4."""
    ffmpeg = _get_ffmpeg()

    list_path = output_path + ".list.txt"
    with open(list_path, "w") as f:
        for seg in segment_paths:
            safe = seg.replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    cmd = [
        ffmpeg, "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"FFmpeg concat failed: {result.stderr[:300]}",
        )


# ---------------------------------------------------------------------------
# Transform service call (with retry)
# ---------------------------------------------------------------------------

async def _call_transform(file_bytes: bytes, filename: str) -> httpx.Response:
    url = f"{BASE_URL}/api/transform"
    headers = {"Authorization": f"Bearer {TRANSFORM_SERVICE_TOKEN}"}

    resp = None
    for attempt in range(1 + MAX_RETRIES):
        resp = await http_client.post(
            url,
            headers=headers,
            files={"clip": (filename, file_bytes, "video/mp4")},
        )
        if resp.status_code not in RETRYABLE_CODES or attempt == MAX_RETRIES:
            return resp
        await asyncio.sleep(RETRY_DELAY * (attempt + 1))

    return resp  # type: ignore[return-value]


async def _transform_segment(seg_path: str, out_path: str) -> None:
    """Send one segment to the transform service and write the result."""
    with open(seg_path, "rb") as f:
        data = f.read()

    resp = await _call_transform(data, os.path.basename(seg_path))

    if resp.status_code != 200:
        detail = ERROR_MAP.get(
            resp.status_code,
            f"Transform service error ({resp.status_code}).",
        )
        try:
            body = resp.json()
            if "error" in body:
                detail = body["error"]
        except Exception:
            pass
        raise HTTPException(status_code=resp.status_code, detail=detail)

    with open(out_path, "wb") as f:
        f.write(resp.content)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/sample")
async def get_sample():
    """Proxy-download the sample short.mp4 from the transform service."""
    url = f"{BASE_URL}/samples/short.mp4"
    try:
        resp = await http_client.get(url)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=exc.response.status_code,
            detail="Failed to fetch sample video.",
        )
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Could not reach sample service.")

    return Response(
        content=resp.content,
        media_type="video/mp4",
        headers={"Content-Disposition": 'attachment; filename="short.mp4"'},
    )


@app.post("/api/transform")
async def transform_video(clip: UploadFile = File(...)):
    """Accept an MP4 upload of any length, chunk → transform → reassemble."""

    if clip.content_type and clip.content_type != "video/mp4":
        raise HTTPException(status_code=422, detail=ERROR_MAP[422])

    file_bytes = await clip.read()
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail=ERROR_MAP[400])

    workdir = tempfile.mkdtemp(prefix="vtx_")
    try:
        # 1. Save upload to disk
        input_path = os.path.join(workdir, clip.filename or "input.mp4")
        with open(input_path, "wb") as f:
            f.write(file_bytes)

        # 2. Split into segments ≤ MAX_CLIP_BYTES
        segments = await asyncio.to_thread(
            _split_video, input_path, workdir, MAX_CLIP_BYTES
        )

        # 3. Transform each segment sequentially
        transformed: list[str] = []
        for i, seg_path in enumerate(segments):
            out_path = os.path.join(workdir, f"gray_{i:03d}.mp4")
            await _transform_segment(seg_path, out_path)
            transformed.append(out_path)

        # 4. Concatenate transformed segments into one video
        if len(transformed) == 1:
            final_path = transformed[0]
        else:
            final_path = os.path.join(workdir, "final.mp4")
            await asyncio.to_thread(_concat_videos, transformed, final_path)

        # 5. Return the full-length grayscale video
        with open(final_path, "rb") as f:
            output_bytes = f.read()

        return Response(
            content=output_bytes,
            media_type="video/mp4",
            headers={
                "Content-Disposition": 'attachment; filename="transformed.mp4"',
            },
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
