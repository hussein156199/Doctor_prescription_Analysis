# src/medicine_app.py
from __future__ import annotations
import math
import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, render_template, request, send_file

# إعداد logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

# إضافة المسارات
ROOT = Path(__file__).resolve().parent
PARENT_DIR = ROOT.parent

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PARENT_DIR))

# استيراد من pipeline
try:
    from pipeline import analyze_prescription_image, search_medicine_by_name, get_image_preview, \
        crop_image_with_coordinates

    logger.info("✅ تم تحميل pipeline بنجاح")
except ImportError as e:
    logger.error(f"❌ خطأ في تحميل pipeline: {e}")


    def analyze_prescription_image(p, crop_coords=None):
        return {"medicines": [], "total_medicines_found": 0, "status": "error"}


    def search_medicine_by_name(n):
        return None


    def get_image_preview(p):
        return None


    def crop_image_with_coordinates(p, x, y, w, h):
        return None

# إعداد Flask
app = Flask(__name__,
            template_folder=str(PARENT_DIR / "templates"),
            static_folder=str(PARENT_DIR / "static"))

app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
app.config["SECRET_KEY"] = "prescription_analyzer_secret_key"

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
UPLOAD_DIR = PARENT_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
(PARENT_DIR / "temp").mkdir(exist_ok=True)

# قاموس لتخزين وقت رفع الملفات لتنظيفها لاحقاً (اختياري)
uploaded_files = {}


def allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXT


def json_safe(obj):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else round(obj, 2)
    return obj


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def api_search():
    """البحث عن دواء بالاسم"""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "الرجاء إدخال اسم الدواء"}), 400

    logger.info(f"🔍 البحث عن: {name}")

    match = search_medicine_by_name(name)

    if not match:
        return jsonify({
            "ok": True,
            "medicine": None,
            "message": f"لم يتم العثور على دواء باسم '{name}'"
        }), 200

    return jsonify({"ok": True, "medicine": json_safe(match)}), 200


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """رفع الصورة والحصول على معاينة"""
    if "image" not in request.files:
        return jsonify({"ok": False, "error": "لم يتم إرسال الملف"}), 400

    file = request.files["image"]

    if not file or not file.filename:
        return jsonify({"ok": False, "error": "الملف فارغ"}), 400

    if not allowed_file(file.filename):
        return jsonify({"ok": False, "error": "نوع الملف غير مدعوم"}), 400

    # حفظ الملف
    ext = Path(file.filename).suffix.lower()
    safe_name = f"upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.urandom(4).hex()}{ext}"
    filepath = UPLOAD_DIR / safe_name
    file.save(str(filepath))

    # تسجيل وقت الرفع
    uploaded_files[safe_name] = datetime.now()

    # الحصول على معاينة
    preview = get_image_preview(str(filepath))

    return jsonify({
        "ok": True,
        "filename": safe_name,
        "preview": preview,
        "original_size": Path(filepath).stat().st_size
    }), 200


@app.route("/api/prescription", methods=["POST"])
def api_prescription():
    """تحليل الصورة مع إمكانية تحديد منطقة القص"""
    data = request.get_json()

    if not data or "filename" not in data:
        return jsonify({"ok": False, "error": "لم يتم إرسال اسم الملف"}), 400

    filename = data["filename"]
    filepath = UPLOAD_DIR / filename

    if not filepath.exists():
        return jsonify({"ok": False, "error": "الملف غير موجود. يرجى رفع الصورة مرة أخرى"}), 404

    # الحصول على إحداثيات القص (اختياري)
    crop_coords = data.get("crop_coords")

    try:
        result = analyze_prescription_image(str(filepath), crop_coords)

        if "medicines" not in result:
            result["medicines"] = []
        if "total_medicines_found" not in result:
            result["total_medicines_found"] = len(result.get("medicines", []))

        return jsonify({"ok": True, **json_safe(result)}), 200

    except Exception as e:
        logger.exception(f"خطأ في التحليل: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

    # ملاحظة: لم نعد نحذف الملف هنا! ليظل متاحاً للتحليل مرة أخرى


@app.route("/api/delete-file", methods=["POST"])
def api_delete_file():
    """حذف ملف مرفوع (اختياري - يمكن استدعاؤه عند مغادرة الصفحة)"""
    data = request.get_json()
    filename = data.get("filename")

    if filename:
        filepath = UPLOAD_DIR / filename
        if filepath.exists():
            filepath.unlink()
            if filename in uploaded_files:
                del uploaded_files[filename]
            return jsonify({"ok": True}), 200

    return jsonify({"ok": False, "error": "الملف غير موجود"}), 404


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "status": "running"}), 200


# تنظيف الملفات القديمة كل ساعة (اختياري - يحذف الملفات الأقدم من ساعة)
def cleanup_old_files():
    """حذف الملفات القديمة كل ساعة للحفاظ على مساحة التخزين"""
    now = datetime.now()
    for filename, upload_time in list(uploaded_files.items()):
        # حذف الملفات الأقدم من ساعة
        if (now - upload_time).seconds > 3600:
            filepath = UPLOAD_DIR / filename
            if filepath.exists():
                filepath.unlink()
            del uploaded_files[filename]
            logger.info(f"🗑️ تم حذف الملف القديم: {filename}")


# تشغيل مهمة التنظيف كل ساعة
import threading


def schedule_cleanup():
    """جدولة تنظيف الملفات كل ساعة"""

    def cleanup_job():
        while True:
            import time
            time.sleep(3600)  # كل ساعة
            cleanup_old_files()

    thread = threading.Thread(target=cleanup_job, daemon=True)
    thread.start()


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("🚀 تشغيل تطبيق تحليل الروشتات الطبية")
    print("=" * 60)
    print(f"📍 http://localhost:5000")
    print(f"📁 مجلد التحميلات: {UPLOAD_DIR}")
    print(f"📁 مجلد المؤقت: {PARENT_DIR / 'temp'}")
    print("=" * 60)
    print("💡 ملاحظة: الملفات المرفوعة تبقى متاحة لمدة ساعة ثم تُحذف تلقائياً")
    print("=" * 60 + "\n")

    # تشغيل مهمة التنظيف التلقائي
    schedule_cleanup()

    app.run(host="0.0.0.0", port=5000, debug=False)