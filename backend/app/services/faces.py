from __future__ import annotations

import json
import math
import urllib.request
from pathlib import Path

from backend.app.config import settings

# SFace (OpenCV) usa cosseno. 0.363 é o limiar do exemplo oficial.
AUTO_THRESHOLD = 0.50
REVIEW_THRESHOLD = 0.363
MARGIN = 0.06

_YUNET = "face_detection_yunet_2023mar.onnx"
_SFACE = "face_recognition_sface_2021dec.onnx"
_URLS = {
    _YUNET: "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    _SFACE: "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}

_detector = None
_recognizer = None


class FaceError(RuntimeError):
    pass


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


def decide(scores: list[tuple[int, float]]) -> dict:
    """scores: (speaker_id, similaridade). Sem LLM."""
    if not scores:
        return {
            "speaker_id": None,
            "confidence": 0.0,
            "status": "unknown",
            "note": "Nenhum palestrante com rosto cadastrado.",
        }
    ordered = sorted(scores, key=lambda item: item[1], reverse=True)
    best_id, best = ordered[0]
    second = ordered[1][1] if len(ordered) > 1 else 0.0
    close = len(ordered) > 1 and (best - second) < MARGIN
    if best >= AUTO_THRESHOLD and not close:
        status = "auto"
        speaker_id = best_id
        note = f"match local {best:.2f}"
    elif best >= REVIEW_THRESHOLD:
        status = "needs_review"
        speaker_id = best_id
        note = f"match duvidoso {best:.2f}" + (" · dois rostos próximos" if close else "")
    else:
        status = "unknown"
        speaker_id = None
        note = f"sem match local (melhor {best:.2f})"
    return {
        "speaker_id": speaker_id,
        "confidence": round(best, 4),
        "status": status,
        "note": note,
    }


def dumps_embedding(values: list[float]) -> str:
    return json.dumps(values)


def loads_embedding(raw: str) -> list[float] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not data:
        return None
    return [float(item) for item in data]


def _model_path(name: str) -> Path:
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.models_dir / name
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = _URLS[name]
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise FaceError(f"não foi possível baixar o modelo local {name}: {exc}") from exc
    if not dest.exists() or dest.stat().st_size == 0:
        raise FaceError(f"modelo local vazio: {name}")
    return dest


def _engines():
    global _detector, _recognizer
    if _detector is not None and _recognizer is not None:
        return _detector, _recognizer
    try:
        import cv2
    except ImportError as exc:
        raise FaceError("opencv não instalado no .venv") from exc
    detector = cv2.FaceDetectorYN.create(str(_model_path(_YUNET)), "", (320, 320), 0.6, 0.3, 5000)
    recognizer = cv2.FaceRecognizerSF.create(str(_model_path(_SFACE)), "")
    _detector, _recognizer = detector, recognizer
    return detector, recognizer


def _load_bgr(path: Path):
    import cv2
    import numpy as np

    image = cv2.imread(str(path))
    if image is not None:
        return image
    try:
        from PIL import Image
    except ImportError as exc:
        raise FaceError(f"não li a imagem {path.name}") from exc
    try:
        rgb = Image.open(path).convert("RGB")
    except OSError as exc:
        raise FaceError(f"imagem ilegível: {path.name}") from exc
    return cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)


def _largest_face(faces):
    return max(faces, key=lambda face: float(face[2]) * float(face[3]))


def crop_face(image, face, margin: float = 0.45):
    height, width = image.shape[:2]
    x, y, box_w, box_h = [float(value) for value in face[:4]]
    pad_x = box_w * margin
    pad_y = box_h * margin
    left = max(0, int(x - pad_x))
    top = max(0, int(y - pad_y))
    right = min(width, int(x + box_w + pad_x))
    bottom = min(height, int(y + box_h + pad_y))
    if right <= left or bottom <= top:
        return image
    return image[top:bottom, left:right]


def analyze_image(path: Path) -> tuple[list[float] | None, object | None, str]:
    """Retorna embedding, recorte BGR e aviso. Sem rosto: embedding None."""
    import cv2

    image = _load_bgr(path)
    if image is None or image.size == 0:
        return None, None, "imagem vazia ou ilegível"
    detector, recognizer = _engines()
    height, width = image.shape[:2]
    detector.setInputSize((width, height))
    _, faces = detector.detect(image)
    if faces is None or len(faces) == 0:
        return None, None, "nenhum rosto detectado; use um retrato de frente"
    face = _largest_face(faces)
    aligned = recognizer.alignCrop(image, face)
    feature = recognizer.feature(aligned)
    embedding = feature.flatten().astype(float).tolist()
    return embedding, crop_face(image, face), ""


def save_crop(crop, dest: Path) -> None:
    import cv2

    dest.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(dest), crop):
        raise FaceError(f"não gravei o recorte em {dest}")
