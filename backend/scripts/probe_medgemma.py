import os, time, io, base64, httpx
from PIL import Image

BASE = "http://host.docker.internal:11434"
MODEL = os.environ.get("MODEL", "medgemma:27b")


def png(n=768):
    im = Image.new("L", (n, n), 128)
    for i in range(0, n, 64):
        for j in range(0, n, 64):
            im.putpixel((i, j), 220)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


img = png()
prompt = (
    "You are a radiologist. For each axial CT image, list any abnormality you see as "
    'JSON {"anomalies":[{"z":int,"finding":str}],"reviewed":[int]}. Slices: '
    + ", ".join(f"z={i}" for i in range(6))
)


def timed(k):
    imgs = [base64.b64encode(img).decode() for _ in range(k)]
    p = {"model": MODEL, "prompt": prompt, "stream": False, "format": "json",
         "options": {"temperature": 0.1, "num_ctx": 8192}, "images": imgs}
    t = time.time()
    r = httpx.post(f"{BASE}/api/generate", json=p, timeout=600)
    dt = time.time() - t
    d = r.json()
    print(f"  images={k:2d}  {dt:6.1f}s  eval_count={d.get('eval_count')}  "
          f"prompt_eval={d.get('prompt_eval_count')}  total_dur={d.get('total_duration', 0)/1e9:.1f}s")
    return dt


print("warmup (loads model into VRAM)...")
timed(1)
print("timed runs:")
for k in (1, 2, 6):
    timed(k)
