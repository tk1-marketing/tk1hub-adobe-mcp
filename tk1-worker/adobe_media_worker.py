#!/usr/bin/env python3
"""
TK1 Hub — Adobe Media Worker
SP5 · Dias 3-5 · Dev 1

Worker local que:
1. Faz polling em media_jobs (Supabase) via claim_next_media_job RPC
2. Processa jobs PS/Illustrator via adb-mcp socket_client
3. Faz upload do resultado para R2 (bucket tk1-media)
4. Chama complete_media_job para marcar done/failed

Job types tratados: ps, illustrator, compress, watermark, crop, color_grade, batch
"""

import os
import sys
import time
import json
import uuid
import logging
import tempfile
import traceback
from pathlib import Path
from datetime import datetime

# Adicionar mcp/ ao path para usar socket_client e core do adb-mcp
WORKER_DIR = Path(__file__).parent
REPO_ROOT   = WORKER_DIR.parent
MCP_DIR     = REPO_ROOT / "mcp"
sys.path.insert(0, str(MCP_DIR))

import socket_client as sc
import boto3
from botocore.client import Config
from supabase import create_client, Client
from dotenv import load_dotenv

# Carregar .env local se existir
load_dotenv(WORKER_DIR / ".env")

# ─── Logging ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("adobe-worker")

# ─── Env ────────────────────────────────────────────────────
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET     = os.environ.get("R2_MEDIA_BUCKET", "tk1-media")
PROXY_URL     = os.environ.get("ADOBE_PROXY_URL", "http://localhost:3001")
PROXY_TIMEOUT = int(os.environ.get("ADOBE_PROXY_TIMEOUT", "120"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "3"))
WORKER_ID     = os.environ.get("WORKER_ID", f"adobe-worker-{uuid.uuid4().hex[:8]}")

# ─── Clientes ───────────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

r2 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)

# ─── Helpers ────────────────────────────────────────────────

def send_cmd(app: str, action: str, options: dict) -> dict:
    """Envia comando para o app Adobe via proxy. Segue o padrão core.py do adb-mcp."""
    sc.configure(app=app, url=PROXY_URL, timeout=PROXY_TIMEOUT)
    command = {"application": app, "action": action, "options": options}
    return sc.send_message_blocking(command)


def upload_r2(local_path: str, r2_key: str) -> str:
    """Faz upload de arquivo local para R2, retorna r2://bucket/key."""
    ext = Path(local_path).suffix.lower()
    content_types = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".svg": "image/svg+xml", ".pdf": "application/pdf",
        ".mp4": "video/mp4", ".mp3": "audio/mpeg",
    }
    ct = content_types.get(ext, "application/octet-stream")
    with open(local_path, "rb") as f:
        r2.upload_fileobj(f, R2_BUCKET, r2_key, ExtraArgs={"ContentType": ct})
    return f"r2://{R2_BUCKET}/{r2_key}"


def save_asset(job_id: str, client_id, r2_path: str,
               asset_type: str, fmt: str, origin: str,
               title: str = None, meta: dict = None,
               is_final: bool = True) -> str:
    row = {
        "job_id":    job_id,
        "client_id": client_id,
        "asset_type": asset_type,
        "format":    fmt,
        "r2_path":   r2_path,
        "origin":    origin,
        "is_final":  is_final,
        "title":     title,
        "metadata":  meta or {},
    }
    res = supabase.table("media_assets").insert(row).execute()
    return res.data[0]["id"]


def complete_job(job_id: str, status: str, output: dict = None, error: str = None):
    supabase.rpc("complete_media_job", {
        "p_job_id": job_id,
        "p_status": status,
        "p_output": json.dumps(output or {}),
        "p_error":  error,
    }).execute()
    log.info(f"Job {job_id[:8]}… → {status}")


def fetch_input_url(url: str, dest_path: str):
    import urllib.request
    urllib.request.urlretrieve(url, dest_path)

# ─── Handlers ───────────────────────────────────────────────

def handle_ps(job: dict):
    """
    PS jobs. input_payload:
    { "action": "export_png"|"resize"|"custom_script",
      "input_url": "https://...",  (opcional)
      "options": {} }
    """
    payload   = job["input_payload"]
    client_id = job["client_id"]
    job_id    = job["id"]
    action    = payload.get("action", "export_png")
    options   = payload.get("options", {})

    with tempfile.TemporaryDirectory() as tmp:
        # Abrir documento de entrada, se fornecido
        if payload.get("input_url"):
            suffix = Path(payload["input_url"]).suffix or ".png"
            inp = os.path.join(tmp, f"input{suffix}")
            fetch_input_url(payload["input_url"], inp)
            # Normalizar para forward slashes (PS espera path posix-like)
            inp_fwd = inp.replace("\\", "/")
            send_cmd("photoshop", "open_document", {"path": inp_fwd})

        out_path = os.path.join(tmp, "output.png")
        out_fwd  = out_path.replace("\\", "/")

        if action == "export_png":
            send_cmd("photoshop", "export_document", {"path": out_fwd, "format": "PNG", **options})

        elif action == "resize":
            send_cmd("photoshop", "resize_image", options)
            send_cmd("photoshop", "export_document", {"path": out_fwd, "format": "PNG"})

        elif action == "custom_script":
            send_cmd("photoshop", "run_script", {"script": options.get("script", "")})
            # Se o script exportou para path customizado, use esse
            custom_out = options.get("output_path")
            if custom_out and os.path.exists(custom_out):
                out_path = custom_out
                out_fwd  = custom_out
            else:
                send_cmd("photoshop", "export_document", {"path": out_fwd, "format": "PNG"})
        else:
            raise ValueError(f"PS action desconhecida: {action}")

        ts     = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        r2_key = f"media/{client_id or 'global'}/{job_id}/{ts}_output.png"
        upload_r2(out_path, r2_key)
        save_asset(job_id=job_id, client_id=client_id, r2_path=r2_key,
                   asset_type="image", fmt="png", origin="ps_runner",
                   title=f"PS Export {ts}", meta={"action": action})
        complete_job(job_id, "done", output={"r2_path": r2_key})


def handle_illustrator(job: dict):
    """
    Illustrator jobs. input_payload:
    { "action": "export_svg"|"export_pdf"|"run_script",
      "input_url": "https://...",  (opcional)
      "options": {} }
    """
    payload   = job["input_payload"]
    client_id = job["client_id"]
    job_id    = job["id"]
    action    = payload.get("action", "export_svg")
    options   = payload.get("options", {})

    with tempfile.TemporaryDirectory() as tmp:
        if payload.get("input_url"):
            inp = os.path.join(tmp, "input.ai")
            fetch_input_url(payload["input_url"], inp)
            inp_fwd = inp.replace("\\", "/")
            # Illustrator: abrir via ExtendScript
            open_script = f'app.open(new File("{inp_fwd}")); "ok"'
            send_cmd("illustrator", "run_script", {"script": open_script})

        if action == "export_svg":
            out_path = os.path.join(tmp, "output.svg")
            out_fwd  = out_path.replace("\\", "/")
            script = (
                f'var doc = app.activeDocument;'
                f'var opts = new ExportOptionsSVG();'
                f'opts.embedRasterImages = true;'
                f'doc.exportFile(new File("{out_fwd}"), ExportType.SVG, opts);'
                f'"ok"'
            )
            send_cmd("illustrator", "run_script", {"script": script})
            fmt = "svg"

        elif action == "export_pdf":
            out_path = os.path.join(tmp, "output.pdf")
            out_fwd  = out_path.replace("\\", "/")
            script = (
                f'var doc = app.activeDocument;'
                f'var opts = new PDFSaveOptions();'
                f'doc.saveAs(new File("{out_fwd}"), opts);'
                f'"ok"'
            )
            send_cmd("illustrator", "run_script", {"script": script})
            fmt = "pdf"

        elif action == "run_script":
            result = send_cmd("illustrator", "run_script",
                               {"script": options.get("script", '"ok"')})
            out_path = options.get("output_path")
            fmt      = options.get("format", "svg")
            if not out_path or not os.path.exists(out_path):
                complete_job(job_id, "done", output={"result": str(result)})
                return
        else:
            raise ValueError(f"Illustrator action desconhecida: {action}")

        ts     = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        r2_key = f"media/{client_id or 'global'}/{job_id}/{ts}_output.{fmt}"
        upload_r2(out_path, r2_key)
        save_asset(job_id=job_id, client_id=client_id, r2_path=r2_key,
                   asset_type="vector", fmt=fmt, origin="illustrator_runner",
                   title=f"AI Export {ts}")
        complete_job(job_id, "done", output={"r2_path": r2_key})


def handle_compress(job: dict):
    """
    Compressão com Pillow (sem Adobe).
    input_payload: { "input_url": "...", "quality": 85, "format": "webp" }
    """
    from PIL import Image

    payload   = job["input_payload"]
    client_id = job["client_id"]
    job_id    = job["id"]
    fmt       = payload.get("format", "webp").lower()
    quality   = int(payload.get("quality", 85))

    with tempfile.TemporaryDirectory() as tmp:
        suffix    = Path(payload["input_url"]).suffix or ".png"
        inp       = os.path.join(tmp, f"input{suffix}")
        out_path  = os.path.join(tmp, f"output.{fmt}")
        fetch_input_url(payload["input_url"], inp)

        img = Image.open(inp).convert("RGB")
        img.save(out_path, format=fmt.upper(), quality=quality, optimize=True)

        ts     = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        r2_key = f"media/{client_id or 'global'}/{job_id}/{ts}_compressed.{fmt}"
        upload_r2(out_path, r2_key)
        save_asset(job_id=job_id, client_id=client_id, r2_path=r2_key,
                   asset_type="image", fmt=fmt, origin="compress_runner")
        complete_job(job_id, "done", output={"r2_path": r2_key})


def handle_watermark(job: dict):
    """
    Watermark com Pillow.
    input_payload: { "input_url": "...", "logo_url": "...", "opacity": 0.5, "position": "bottom-right" }
    """
    from PIL import Image

    payload  = job["input_payload"]
    client_id = job["client_id"]
    job_id   = job["id"]
    opacity  = float(payload.get("opacity", 0.5))
    position = payload.get("position", "bottom-right")

    with tempfile.TemporaryDirectory() as tmp:
        base_p = os.path.join(tmp, "base.png")
        logo_p = os.path.join(tmp, "logo.png")
        out_p  = os.path.join(tmp, "watermarked.png")

        fetch_input_url(payload["input_url"], base_p)
        fetch_input_url(payload["logo_url"],  logo_p)

        base = Image.open(base_p).convert("RGBA")
        logo = Image.open(logo_p).convert("RGBA")

        logo_w = base.width // 5
        ratio  = logo_w / logo.width
        logo   = logo.resize((logo_w, int(logo.height * ratio)), Image.LANCZOS)

        r, g, b, a = logo.split()
        a = a.point(lambda x: int(x * opacity))
        logo = Image.merge("RGBA", (r, g, b, a))

        margin = 20
        pos_map = {
            "bottom-right": (base.width - logo.width - margin, base.height - logo.height - margin),
            "bottom-left":  (margin, base.height - logo.height - margin),
            "top-right":    (base.width - logo.width - margin, margin),
            "top-left":     (margin, margin),
            "center":       ((base.width - logo.width) // 2, (base.height - logo.height) // 2),
        }
        pos = pos_map.get(position, pos_map["bottom-right"])

        base.paste(logo, pos, logo)
        base.save(out_p, "PNG")

        ts     = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        r2_key = f"media/{client_id or 'global'}/{job_id}/{ts}_watermarked.png"
        upload_r2(out_p, r2_key)
        save_asset(job_id=job_id, client_id=client_id, r2_path=r2_key,
                   asset_type="image", fmt="png", origin="watermark_runner")
        complete_job(job_id, "done", output={"r2_path": r2_key})


def handle_crop(job: dict):
    """
    Recorte com Pillow.
    input_payload: { "input_url": "...", "left": 0, "top": 0, "right": 100, "bottom": 100 }
    """
    from PIL import Image

    payload  = job["input_payload"]
    client_id = job["client_id"]
    job_id   = job["id"]

    with tempfile.TemporaryDirectory() as tmp:
        suffix = Path(payload["input_url"]).suffix or ".png"
        inp    = os.path.join(tmp, f"input{suffix}")
        out_p  = os.path.join(tmp, "cropped.png")
        fetch_input_url(payload["input_url"], inp)

        img  = Image.open(inp)
        box  = (int(payload["left"]), int(payload["top"]),
                int(payload["right"]), int(payload["bottom"]))
        crop = img.crop(box)
        crop.save(out_p, "PNG")

        ts     = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        r2_key = f"media/{client_id or 'global'}/{job_id}/{ts}_cropped.png"
        upload_r2(out_p, r2_key)
        save_asset(job_id=job_id, client_id=client_id, r2_path=r2_key,
                   asset_type="image", fmt="png", origin="crop_runner")
        complete_job(job_id, "done", output={"r2_path": r2_key})


def handle_color_grade(job: dict):
    """
    Color grading básico com Pillow (brightness + contrast + saturation).
    input_payload: { "input_url": "...", "brightness": 1.0, "contrast": 1.0, "saturation": 1.0 }
    """
    from PIL import Image, ImageEnhance

    payload  = job["input_payload"]
    client_id = job["client_id"]
    job_id   = job["id"]

    with tempfile.TemporaryDirectory() as tmp:
        suffix = Path(payload["input_url"]).suffix or ".png"
        inp    = os.path.join(tmp, f"input{suffix}")
        out_p  = os.path.join(tmp, "graded.png")
        fetch_input_url(payload["input_url"], inp)

        img = Image.open(inp).convert("RGB")
        if (v := float(payload.get("brightness", 1.0))) != 1.0:
            img = ImageEnhance.Brightness(img).enhance(v)
        if (v := float(payload.get("contrast", 1.0))) != 1.0:
            img = ImageEnhance.Contrast(img).enhance(v)
        if (v := float(payload.get("saturation", 1.0))) != 1.0:
            img = ImageEnhance.Color(img).enhance(v)
        img.save(out_p, "PNG")

        ts     = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        r2_key = f"media/{client_id or 'global'}/{job_id}/{ts}_graded.png"
        upload_r2(out_p, r2_key)
        save_asset(job_id=job_id, client_id=client_id, r2_path=r2_key,
                   asset_type="image", fmt="png", origin="color_grade_runner")
        complete_job(job_id, "done", output={"r2_path": r2_key})


def handle_batch(job: dict):
    """
    Processamento em lote: aplica o mesmo handler a múltiplos inputs.
    input_payload: { "batch_type": "compress", "items": [...], "shared_options": {} }
    """
    payload  = job["input_payload"]
    job_id   = job["id"]
    batch_type = payload.get("batch_type", "compress")
    items      = payload.get("items", [])

    handler = HANDLERS.get(batch_type)
    if not handler:
        raise ValueError(f"batch_type '{batch_type}' sem handler")

    results = []
    for i, item_payload in enumerate(items):
        fake_job = {**job, "input_payload": {**payload.get("shared_options", {}), **item_payload}}
        try:
            handler(fake_job)
            results.append({"index": i, "status": "done"})
        except Exception as e:
            results.append({"index": i, "status": "failed", "error": str(e)})

    all_ok = all(r["status"] == "done" for r in results)
    complete_job(job_id, "done" if all_ok else "failed",
                 output={"results": results},
                 error=None if all_ok else f"{sum(1 for r in results if r['status']=='failed')} items falharam")


# ─── Dispatcher ─────────────────────────────────────────────
HANDLERS = {
    "ps":          handle_ps,
    "illustrator": handle_illustrator,
    "compress":    handle_compress,
    "watermark":   handle_watermark,
    "crop":        handle_crop,
    "color_grade": handle_color_grade,
    "batch":       handle_batch,
}

JOB_TYPES = list(HANDLERS.keys())


def process_job(job: dict):
    job_type = job["job_type"]
    job_id   = job["id"]
    log.info(f"Processando job {job_id[:8]}… type={job_type}")

    handler = HANDLERS.get(job_type)
    if not handler:
        complete_job(job_id, "failed",
                     error=f"job_type '{job_type}' sem handler implementado")
        return

    try:
        handler(job)
    except Exception as e:
        tb = traceback.format_exc()
        log.error(f"Job {job_id[:8]}… FAILED:\n{tb}")
        complete_job(job_id, "failed", error=str(e))


# ─── Main loop ──────────────────────────────────────────────
def main():
    log.info(f"TK1 Adobe Media Worker iniciado — worker_id={WORKER_ID}")
    log.info(f"Proxy: {PROXY_URL} | R2: {R2_BUCKET} | Poll: {POLL_INTERVAL}s")
    log.info(f"Job types: {', '.join(JOB_TYPES)}")

    while True:
        try:
            claimed = False
            for job_type in JOB_TYPES:
                res = supabase.rpc("claim_next_media_job", {
                    "p_job_type":  job_type,
                    "p_worker_id": WORKER_ID,
                }).execute()

                if res.data:
                    process_job(res.data[0])
                    claimed = True
                    break

            if not claimed:
                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log.info("Worker encerrado pelo usuário.")
            break
        except Exception as e:
            log.error(f"Erro no loop principal: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
