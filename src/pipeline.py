# src/pipeline.py
import cv2
import re
import numpy as np
import pytesseract
from pathlib import Path
import logging
from typing import List, Dict, Optional, Tuple
import sys
import time
from concurrent.futures import ThreadPoolExecutor

current_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(current_dir))

pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    from drugs import find_drug_with_details, get_all_medicine_names, search_medicine_by_name, exact_match

    logger.info("✅ تم استيراد drugs.py بنجاح")
except ImportError as e:
    logger.error(f"❌ خطأ في استيراد drugs.py: {e}")


    def find_drug_with_details(name):
        if name:
            return {"Name": name, "confidence": 50, "alternatives": []}
        return None


    def get_all_medicine_names():
        return []


    def search_medicine_by_name(name):
        if name:
            return {"Name": name, "confidence": 50, "alternatives": []}
        return None


    def exact_match(name):
        return None

# ذاكرة تخزين مؤقت للـ OCR
ocr_cache = {}


def read_image(path: str) -> np.ndarray:
    try:
        data = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        return img
    except Exception as e:
        logger.error(f"خطأ في قراءة الصورة: {e}")
        return None


def save_image(image: np.ndarray, path: str):
    cv2.imwrite(path, image)


def crop_image_with_coordinates(image_path: str, x: int, y: int, w: int, h: int) -> np.ndarray:
    image = read_image(image_path)
    if image is None:
        raise Exception(f"لا يمكن قراءة الصورة: {image_path}")
    cropped = image[y:y + h, x:x + w]
    return cropped


def preprocess_image(image: np.ndarray) -> np.ndarray:
    if image is None:
        return None

    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image.copy()

    # تقليل حجم الصورة للسرعة
    h, w = gray.shape
    if w > 1500:
        scale = 1500 / w
        new_w = int(w * scale)
        new_h = int(h * scale)
        gray = cv2.resize(gray, (new_w, new_h))

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    return gray


def split_into_text_lines(image: np.ndarray) -> List[np.ndarray]:
    if image is None:
        return []

    h, w = image.shape[:2]
    processed = preprocess_image(image)

    if processed is None:
        return [image]

    horizontal_projection = np.sum(processed == 0, axis=1)

    lines = []
    in_line = False
    line_start = 0
    min_line_height = 12
    min_text_density = w * 0.03

    for i, proj in enumerate(horizontal_projection):
        if proj > min_text_density:
            if not in_line:
                in_line = True
                line_start = max(0, i - 2)
        else:
            if in_line:
                in_line = False
                line_end = min(h, i + 2)
                if line_end - line_start >= min_line_height:
                    lines.append(image[line_start:line_end, :])

    if in_line:
        lines.append(image[line_start:h, :])

    if not lines:
        lines = [image]

    logger.info(f"تم تقسيم الصورة إلى {len(lines)} أسطر")
    return lines


def extract_text_from_line(line: np.ndarray) -> str:
    if line is None or line.size == 0:
        return ""

    # التحقق من التخزين المؤقت (باستخدام هاش الصورة)
    line_hash = hash(line.tobytes())
    if line_hash in ocr_cache:
        return ocr_cache[line_hash]

    processed = preprocess_image(line)
    if processed is None:
        return ""

    h, w = processed.shape
    if w < 400:
        scale = max(1.5, 500 / w)
        new_w = int(w * scale)
        new_h = int(h * scale)
        processed = cv2.resize(processed, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    try:
        # استخدام PSM واحد فقط للسرعة
        config = r'--oem 3 --psm 7'
        text = pytesseract.image_to_string(processed, config=config)
        text = text.strip()

        text = re.sub(r'[^a-zA-Z0-9\s]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        ocr_cache[line_hash] = text
        return text

    except Exception as e:
        logger.error(f"خطأ في OCR: {e}")
        return ""


def correct_ocr_text(text: str) -> str:
    if not text:
        return text

    corrections = {
        'O': '0', 'Omg': '40mg', 'omg': '40mg',
        'Ome': '40me', 'ome': '40me',
        'Cansule': 'Capsule', 'cansule': 'capsule',
        'go': '', 'jo': '', 'eo': '',
    }

    corrected = text
    for wrong, correct in corrections.items():
        corrected = corrected.replace(wrong, correct)

    numbers = re.findall(r'\d+', corrected)
    if numbers and 'mg' in corrected.lower():
        corrected = re.sub(r'Omg|omg', f'{numbers[0]}mg', corrected, flags=re.IGNORECASE)

    corrected = re.sub(r'\s+', ' ', corrected).strip()
    return corrected


def try_multiple_searches(text: str) -> Optional[Dict]:
    if not text:
        return None

    # 1. النص الأصلي
    result = find_drug_with_details(text)
    if result and result.get("Name"):
        logger.info(f"   ✅ تم العثور على: {result['Name']} (النص الأصلي)")
        return result

    # 2. النص المصحح
    corrected = correct_ocr_text(text)
    if corrected != text:
        result = find_drug_with_details(corrected)
        if result and result.get("Name"):
            logger.info(f"   ✅ تم العثور على: {result['Name']} (بعد التصحيح: '{corrected}')")
            return result

    return None


def analyze_prescription_image(image_path: str, crop_coords: dict = None) -> Dict:
    start_time = time.time()

    logger.info(f"بدء تحليل الصورة: {image_path}")

    try:
        if crop_coords and all(k in crop_coords for k in ['x', 'y', 'width', 'height']):
            logger.info(
                f"استخدام إحداثيات القص: x={crop_coords['x']}, y={crop_coords['y']}, w={crop_coords['width']}, h={crop_coords['height']}")
            region = crop_image_with_coordinates(
                image_path,
                crop_coords['x'],
                crop_coords['y'],
                crop_coords['width'],
                crop_coords['height']
            )
        else:
            region = read_image(image_path)
            if region is None:
                raise Exception(f"لا يمكن قراءة الصورة: {image_path}")

        lines = split_into_text_lines(region)

        medicines_found = []
        processed_lines = []

        # معالجة متوازية للأسطر (أسرع)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            for idx, line in enumerate(lines):
                futures.append(executor.submit(process_single_line, idx, line))

            for future in futures:
                result = future.result()
                if result:
                    processed_lines.append(result)
                    if result.get("medicine"):
                        medicines_found.append(result["medicine"])

        # إزالة التكرار مع الاحتفاظ بالثقة الأعلى
        unique_medicines = []
        seen_names = {}
        for med in medicines_found:
            name = med.get("Name", "")
            confidence = med.get("confidence", 0)
            if name:
                if name not in seen_names or confidence > seen_names[name].get("confidence", 0):
                    seen_names[name] = med

        unique_medicines = list(seen_names.values())

        elapsed_time = round(time.time() - start_time, 2)

        logger.info(f"\n{'=' * 40}")
        logger.info(f"✅ اكتمل التحليل: {len(unique_medicines)} دواء في {elapsed_time} ثانية")

        return {
            "medicines": unique_medicines,
            "lines": processed_lines,
            "total_medicines_found": len(unique_medicines),
            "total_lines_processed": len(lines),
            "processing_time": elapsed_time,
            "status": "success"
        }

    except Exception as e:
        logger.error(f"خطأ في التحليل: {e}")
        import traceback
        traceback.print_exc()
        return {
            "medicines": [],
            "lines": [],
            "total_medicines_found": 0,
            "total_lines_processed": 0,
            "processing_time": 0,
            "status": "error",
            "error": str(e)
        }


def process_single_line(idx: int, line: np.ndarray) -> Dict:
    """معالجة سطر واحد - تستخدم في المعالجة المتوازية"""
    logger.info(f"📝 معالجة السطر {idx + 1}:")

    raw_text = extract_text_from_line(line)
    logger.info(f"   📖 النص المستخرج: '{raw_text}'")

    if not raw_text:
        return {"line_index": idx, "ocr_text": "", "medicine": None}

    medicine = try_multiple_searches(raw_text)

    if medicine and medicine.get("Name"):
        logger.info(f"   ✅ تم إضافة الدواء: {medicine['Name']} (الثقة: {medicine.get('confidence', 0)}%)")
        return {
            "line_index": idx,
            "ocr_text": raw_text,
            "matched_text": raw_text,
            "medicine": medicine
        }
    else:
        logger.info(f"   ❌ لم يتم العثور على دواء مطابق")
        return {
            "line_index": idx,
            "ocr_text": raw_text,
            "matched_text": None,
            "medicine": None
        }


def get_image_preview(image_path: str) -> str:
    import base64
    image = read_image(image_path)
    if image is None:
        return None

    h, w = image.shape[:2]
    max_size = 800
    if w > max_size:
        scale = max_size / w
        new_w = int(w * scale)
        new_h = int(h * scale)
        image = cv2.resize(image, (new_w, new_h))

    _, buffer = cv2.imencode('.png', image)
    return base64.b64encode(buffer).decode('utf-8')


__all__ = [
    'analyze_prescription_image',
    'search_medicine_by_name',
    'get_image_preview',
    'crop_image_with_coordinates'
]

if __name__ == "__main__":
    temp_dir = Path(__file__).resolve().parent.parent / "temp"
    temp_dir.mkdir(exist_ok=True)

    import sys

    if len(sys.argv) > 1:
        image_path = sys.argv[1]
    else:
        image_path = str(Path(__file__).resolve().parent.parent / "pres.jpeg")

    if Path(image_path).exists():
        result = analyze_prescription_image(image_path)
        print("\n" + "=" * 50)
        print(f"💊 الأدوية المكتشفة: {result['total_medicines_found']}")
        for med in result["medicines"]:
            print(f"\n💊 {med['Name']} (الثقة: {med.get('confidence', 0)}%)")
            print(f"   🧪 {med.get('Contains', '')[:50]}")
            print(f"   📖 {med.get('ProductIntroduction', '')[:50]}")
            print(f"   💪 {med.get('ProductBenefits', '')[:50]}")
            print(f"   ⚠️ {med.get('SideEffect', '')[:50]}")
            print(f"   💊 {med.get('HowToUse', '')[:50]}")
            print(f"   🔬 {med.get('HowWorks', '')[:50]}")
            print(f"   💡 {med.get('QuickTips', '')[:50]}")
            print(f"   🛡️ {med.get('SafetyAdvice', '')[:50]}")
    else:
        print(f"❌ الصورة غير موجودة: {image_path}")