import os, re, json, uuid, time, shutil, asyncio, socket, ipaddress, zipfile, tempfile
from urllib.parse import urlparse, quote
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

app = FastAPI()
SEM = asyncio.Semaphore(int(os.getenv("MAX_JOBS", "3")))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
JOBS, LC, COOK = {}, {}, []
if os.getenv("YT_COOKIES"):
    open("/tmp/cookies.txt", "w").write(os.environ["YT_COOKIES"])
    COOK = ["--cookies", "/tmp/cookies.txt"]


def safe(u):
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "invalid link")
    try:
        for i in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(i[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise HTTPException(400, "blocked address")
    except socket.gaierror:
        raise HTTPException(400, "unknown host")
    return u


async def chk(r):
    safe(str(r.url))


def client(t=30):
    return httpx.AsyncClient(timeout=t, follow_redirects=True, headers={"User-Agent": UA},
                             limits=httpx.Limits(max_connections=20), event_hooks={"request": [chk]})


async def run(*c, timeout=900):
    p = await asyncio.create_subprocess_exec(*c, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        o, e = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        raise HTTPException(504, "timeout")
    return p.returncode, o.decode("utf8", "ignore"), e.decode("utf8", "ignore")


def purge():
    now = time.time()
    for k, (d, _, t) in list(JOBS.items()):
        if now - t > 900:
            shutil.rmtree(d, True)
            JOBS.pop(k, None)


def clean(s, n=60):
    return re.sub(r'[\\/:*?"<>|\r\n]', "", s or "")[:n].strip()


@app.get("/")
def home():
    return FileResponse("index.html")


@app.get("/api/lessons")
async def lessons():
    rows = []
    for ln in open("lessons.txt", encoding="utf8"):
        p = [x.strip() for x in ln.split("|")]
        m = len(p) > 1 and re.search(r"(?:v=|youtu\.be/)([\w-]{11})", p[1])
        if m:
            rows.append((p[0], m.group(1), p[2] if len(p) > 2 else ""))

    async def one(c, i, lang, v, t):
        o = LC.get(v)
        if not o:
            try:
                r = await c.get("https://www.youtube.com/oembed", params={"url": "https://youtu.be/" + v, "format": "json"})
                o = r.json() if r.status_code == 200 else {}
            except Exception:
                o = {}
            if o:
                LC[v] = o
        return {"id": v, "lang": lang, "title": t or o.get("title") or f"វីដេអូ {i}",
                "by": o.get("author_name", ""), "thumb": f"https://i.ytimg.com/vi/{v}/hqdefault.jpg"}

    async with httpx.AsyncClient(timeout=10) as c:
        return await asyncio.gather(*[one(c, i, *r) for i, r in enumerate(rows, 1)])


@app.post("/api/info")
async def info(req: Request):
    url = safe(((await req.json()).get("url") or "").strip())
    async with SEM:
        code, out, _ = await run("yt-dlp", "-J", "--no-playlist", "--no-warnings", *COOK, url, timeout=90)
        if code == 0:
            try:
                d = json.loads(out)
            except Exception:
                d = {}
            fm = [f for f in d.get("formats") or [] if f.get("vcodec") != "none"]
            if fm:
                hs = sorted({f["height"] for f in fm if f.get("height")})
                return {"type": "video", "title": d.get("title"), "thumb": d.get("thumbnail"),
                        "by": d.get("uploader") or d.get("channel"), "dur": d.get("duration"), "heights": hs}
        code, out, _ = await run("gallery-dl", "-j", url, timeout=90)
    try:
        es = json.loads(out)
    except Exception:
        es = []
    items = []
    for e in es:
        if e[0] == 3:
            ext = ((e[2] if len(e) > 2 else {}).get("extension") or e[1].split("?")[0].rsplit(".", 1)[-1]).lower()
            if ext in ("jpg", "jpeg", "png", "webp", "gif"):
                items.append(e[1])
    if not items:
        raise HTTPException(422, "no video or photo found")
    meta = next((e[1] for e in es if e[0] == 2), {})
    t = meta.get("description") or meta.get("title") or meta.get("username") or "photos"
    return {"type": "photo", "title": " ".join(str(t).split())[:80], "items": items[:100]}


@app.post("/api/prepare")
async def prepare(req: Request):
    j = await req.json()
    url, h, mp3 = safe((j.get("url") or "").strip()), int(j.get("h") or 0), bool(j.get("mp3"))
    purge()
    d = tempfile.mkdtemp()
    a = ["yt-dlp", "--no-playlist", "--no-warnings", "-N", "8", "--max-filesize", "1500M", "--embed-metadata",
         "--embed-thumbnail", "--convert-thumbnails", "jpg", "-o", d + "/%(title).80s.%(ext)s", *COOK]
    if mp3:
        a += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        f = f"[height<={h}]" if h else ""
        a += ["-f", f"bv*{f}+ba/b{f}/b", "--merge-output-format", "mp4/mkv"]
    async with SEM:
        code, _, err = await run(*a, url)
    fs = [os.path.join(d, x) for x in os.listdir(d) if x.rsplit(".", 1)[-1] in ("mp4", "mkv", "webm", "mp3", "m4a")]
    if code or not fs:
        shutil.rmtree(d, True)
        raise HTTPException(422, (err.strip().splitlines() or ["download failed"])[-1][:200])
    p = max(fs, key=os.path.getsize)
    k = uuid.uuid4().hex
    JOBS[k] = (d, p, time.time())
    return {"id": k, "name": os.path.basename(p), "size": os.path.getsize(p)}


@app.get("/api/file/{k}")
def file(k: str):
    j = JOBS.get(k)
    if not j:
        raise HTTPException(404, "file expired, please try again")
    return FileResponse(j[1], filename=os.path.basename(j[1]))


@app.get("/api/img")
async def img(u: str, dl: int = 0, name: str = "photo"):
    safe(u)
    async with client() as c:
        r = await c.get(u)
    ct = r.headers.get("content-type", "")
    if r.status_code != 200 or not ct.startswith("image/") or len(r.content) > 30_000_000:
        raise HTTPException(502, "image unavailable")
    h = {"Cache-Control": "max-age=3600"}
    if dl:
        ext = os.path.splitext(urlparse(u).path)[1] or ".jpg"
        h["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote((clean(name) or "photo") + ext)
    return Response(r.content, media_type=ct, headers=h)


@app.post("/api/zip")
async def zip_(req: Request):
    j = await req.json()
    urls = [safe(u) for u in (j.get("urls") or [])[:100]]
    name = clean(j.get("name")) or "photos"
    purge()
    d = tempfile.mkdtemp()
    path = f"{d}/{name}.zip"
    async with client() as c:
        rs = await asyncio.gather(*[c.get(u) for u in urls], return_exceptions=True)
    n = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for r in rs:
            if isinstance(r, httpx.Response) and r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                n += 1
                z.writestr(f"{name}-{n}{os.path.splitext(urlparse(str(r.url)).path)[1] or '.jpg'}", r.content)
    if not n:
        shutil.rmtree(d, True)
        raise HTTPException(502, "no images could be fetched")
    k = uuid.uuid4().hex
    JOBS[k] = (d, path, time.time())
    return {"id": k, "name": f"{name}.zip", "size": os.path.getsize(path)}
