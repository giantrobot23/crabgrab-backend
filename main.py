from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import yt_dlp
import httpx
import asyncio

app = FastAPI(title="CrabGrab API")

# Allow your frontend domain here
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Replace with ["https://crabgrab.fun"] in production
    allow_methods=["GET"],
    allow_headers=["*"],
)


def get_ydl_opts(quiet=True):
    return {
        "quiet": quiet,
        "no_warnings": True,
        "extract_flat": False,
        "noplaylist": True,
        "cookiefile": "/opt/render/project/src/cookies.txt",
    }


def format_duration(seconds):
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02}:{s:02}"
    return f"{m}:{s:02}"


def pick_formats(info: dict, want_audio_only: bool = False):
    """
    Returns a clean list of format options to show the user.
    """
    formats = info.get("formats", [])
    seen_heights = set()
    result = []

    if want_audio_only:
        # Best audio formats
        audio_fmts = [
            f for f in formats
            if f.get("vcodec") == "none" and f.get("acodec") != "none"
        ]
        audio_fmts.sort(key=lambda f: f.get("abr", 0), reverse=True)
        for f in audio_fmts[:3]:
            abr = int(f.get("abr", 0))
            result.append({
                "format_id": f["format_id"],
                "label": f"{abr}kbps MP3" if abr else "MP3",
                "ext": f.get("ext", "mp3"),
            })
        return result

    # Video formats — deduplicate by height
    video_fmts = [
        f for f in formats
        if f.get("vcodec") != "none" and f.get("height")
    ]
    video_fmts.sort(key=lambda f: f.get("height", 0), reverse=True)

    for f in video_fmts:
        h = f.get("height")
        if h and h not in seen_heights:
            seen_heights.add(h)
            result.append({
                "format_id": f["format_id"],
                "label": f"{h}p",
                "ext": f.get("ext", "mp4"),
            })
        if len(result) >= 5:
            break

    return result


@app.get("/info")
async def get_info(url: str = Query(..., description="Video URL to fetch info for")):
    """
    Returns video metadata + available format options.
    Frontend calls this when user hits 'Grab it'.
    """
    try:
        opts = get_ydl_opts()

        def extract():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        # Run blocking yt-dlp in thread pool
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, extract)

        formats_video = pick_formats(info, want_audio_only=False)
        formats_audio = pick_formats(info, want_audio_only=True)

        return {
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration_string": format_duration(info.get("duration")),
            "extractor": info.get("extractor_key", info.get("extractor", "")),
            "webpage_url": info.get("webpage_url", url),
            "formats": formats_video + formats_audio,
        }

    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch video: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")


@app.get("/download")
async def download_video(
    url: str = Query(...),
    format_id: str = Query("best"),
):
    """
    Streams the video/audio file directly to the browser for download.
    """
    try:
        opts = {
            **get_ydl_opts(),
            "format": format_id,
        }

        def get_direct_url():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # Find the matching format's direct URL
                for f in info.get("formats", []):
                    if f.get("format_id") == format_id:
                        return f.get("url"), f.get("ext", "mp4"), info.get("title", "video")
                # Fallback to best
                return info.get("url"), info.get("ext", "mp4"), info.get("title", "video")

        loop = asyncio.get_event_loop()
        direct_url, ext, title = await loop.run_in_executor(None, get_direct_url)

        if not direct_url:
            raise HTTPException(status_code=404, detail="Could not get download URL")

        # Stream the file from the platform's CDN through to the user
        async def stream():
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                async with client.stream("GET", direct_url) as response:
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        yield chunk

        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()
        filename = f"{safe_title[:60]}.{ext}"

        return StreamingResponse(
            stream(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health():
    return {"status": "ok", "service": "CrabGrab API"}
