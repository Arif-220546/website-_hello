import os
import io
import uuid
import zipfile
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template
from PIL import Image
from pypdf import PdfWriter, PdfReader

try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    from fpdf import FPDF
    HAS_FPDF = True
except ImportError:
    HAS_FPDF = False

app = Flask(__name__)

DOWNLOADS_FOLDER = str(Path.home() / "Downloads")
os.makedirs(DOWNLOADS_FOLDER, exist_ok=True)

IMAGE_EXTS   = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff"}
ALLOWED_INPUT = IMAGE_EXTS | {"pdf", "txt"}
PILLOW_FMT   = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG",
                 "bmp": "BMP", "webp": "WEBP", "tiff": "TIFF", "gif": "GIF"}


import re

def ext(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def unique_path(folder, name):
    """Avoid overwriting existing files by appending a short uuid."""
    base, extension = os.path.splitext(name)
    candidate = os.path.join(folder, name)
    if not os.path.exists(candidate):
        return candidate, name
    short = str(uuid.uuid4())[:8]
    final_name = f"{base}_{short}{extension}"
    return os.path.join(folder, final_name), final_name


def encrypt_pdf(pdf_bytes_io, password):
    reader = PdfReader(pdf_bytes_io)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=password, owner_password=password)
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/convert", methods=["POST"])
def convert():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    out_fmt  = request.form.get("output_format", "pdf").lower().strip(".")
    password = request.form.get("password", "").strip()
    # User-chosen filename: strip unsafe characters, fall back to original name
    save_as_raw = request.form.get("save_as", "").strip()
    save_as = re.sub(r'[\\/:*?"<>|]', '_', save_as_raw) if save_as_raw else None

    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    in_ext = ext(file.filename)
    if in_ext not in ALLOWED_INPUT:
        return jsonify({"error": f"Unsupported input format '.{in_ext}'. Supported: {', '.join(sorted(ALLOWED_INPUT))}"}), 400

    valid_out = IMAGE_EXTS | {"pdf"}
    if out_fmt not in valid_out:
        return jsonify({"error": f"Unsupported output format '{out_fmt}'."}), 400

    if out_fmt == "pdf" and not password:
        return jsonify({"error": "A password is required when converting to PDF."}), 400

    try:
        raw = file.read()
        base = save_as if save_as else (os.path.splitext(file.filename)[0] or "converted")

        # ── Image → PDF ──────────────────────────────────────────────
        if in_ext in IMAGE_EXTS and out_fmt == "pdf":
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PDF", resolution=150)
            buf.seek(0)
            enc = encrypt_pdf(buf, password)
            out_name = f"{base}_protected.pdf"
            out_path, out_name = unique_path(DOWNLOADS_FOLDER, out_name)
            with open(out_path, "wb") as f:
                f.write(enc.read())
            return jsonify({"success": True, "filename": out_name, "saved_to": out_path})

        # ── Image → Image ─────────────────────────────────────────────
        if in_ext in IMAGE_EXTS and out_fmt in IMAGE_EXTS:
            img = Image.open(io.BytesIO(raw))
            pil_fmt = PILLOW_FMT.get(out_fmt, out_fmt.upper())
            if pil_fmt == "JPEG" and img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            out_name = f"{base}.{out_fmt}"
            out_path, out_name = unique_path(DOWNLOADS_FOLDER, out_name)
            img.save(out_path, format=pil_fmt)
            return jsonify({"success": True, "filename": out_name, "saved_to": out_path})

        # ── PDF → Image ───────────────────────────────────────────────
        if in_ext == "pdf" and out_fmt in IMAGE_EXTS:
            if not HAS_FITZ:
                return jsonify({"error": "PyMuPDF not available."}), 500
            doc = fitz.open(stream=raw, filetype="pdf")
            fitz_fmt = "jpeg" if out_fmt in ("jpg", "jpeg") else out_fmt
            mat = fitz.Matrix(2, 2)  # 144 DPI
            if len(doc) == 1:
                pix = doc[0].get_pixmap(matrix=mat)
                img_bytes = pix.tobytes(fitz_fmt)
                out_name = f"{base}.{out_fmt}"
                out_path, out_name = unique_path(DOWNLOADS_FOLDER, out_name)
                with open(out_path, "wb") as f:
                    f.write(img_bytes)
            else:
                out_name = f"{base}_pages.zip"
                out_path, out_name = unique_path(DOWNLOADS_FOLDER, out_name)
                with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, page in enumerate(doc):
                        pix = page.get_pixmap(matrix=mat)
                        zf.writestr(f"page_{i+1:03d}.{out_fmt}", pix.tobytes(fitz_fmt))
            return jsonify({"success": True, "filename": out_name, "saved_to": out_path})

        # ── PDF → PDF (re-protect) ────────────────────────────────────
        if in_ext == "pdf" and out_fmt == "pdf":
            enc = encrypt_pdf(io.BytesIO(raw), password)
            out_name = f"{base}_protected.pdf"
            out_path, out_name = unique_path(DOWNLOADS_FOLDER, out_name)
            with open(out_path, "wb") as f:
                f.write(enc.read())
            return jsonify({"success": True, "filename": out_name, "saved_to": out_path})

        # ── TXT → PDF ────────────────────────────────────────────────
        if in_ext == "txt" and out_fmt == "pdf":
            if not HAS_FPDF:
                return jsonify({"error": "fpdf2 not installed."}), 500
            text = raw.decode("utf-8", errors="replace")
            pdf = FPDF()
            pdf.set_auto_page_break(auto=True, margin=15)
            pdf.add_page()
            pdf.set_font("Helvetica", size=11)
            for line in text.splitlines():
                pdf.multi_cell(0, 8, line or " ")
            pdf_bytes_out = bytes(pdf.output())
            if password:
                enc = encrypt_pdf(io.BytesIO(pdf_bytes_out), password)
                out_name = f"{base}_protected.pdf"
                out_path, out_name = unique_path(DOWNLOADS_FOLDER, out_name)
                with open(out_path, "wb") as f:
                    f.write(enc.read())
            else:
                out_name = f"{base}.pdf"
                out_path, out_name = unique_path(DOWNLOADS_FOLDER, out_name)
                with open(out_path, "wb") as f:
                    f.write(pdf_bytes_out)
            return jsonify({"success": True, "filename": out_name, "saved_to": out_path})

        return jsonify({"error": f"No converter found for .{in_ext} → .{out_fmt}"}), 400

    except Exception as e:
        return jsonify({"error": f"Conversion failed: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
