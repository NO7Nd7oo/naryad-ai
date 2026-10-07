"""AI Engine for НарядAI project (АО «Костанайские минералы»).

Provides AI-powered verification of work orders and before/after photo analysis
using Google Gemini models, with fallback to heuristic rules when the API key
is unavailable or internet access is down.

Typical usage::

    from src.ai_engine import verify_work_order, analyze_photos_before_after

    verdict = verify_work_order(
        problem_desc="Течь в радиаторе",
        work_done="Заменен радиатор, подключены шланги",
        materials_used=["медьной трубопровод", "новый радиатор"],
    )

    with open("before.jpg", "rb") as f:
        before_bytes = f.read()
    with open("after.jpg", "rb") as f:
        after_bytes = f.read()
    result = analyze_photos_before_after(before_bytes, after_bytes)
    print(result)  # {'rating': 5, 'verdict': '...'}
"""

import os
import json
import logging
from typing import List, Dict, Any

import google.generativeai as genai

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Configuration: read Gemini API key from environment
# ------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    _model = genai.GenerativeModel("gemini-1.5-flash")
    _has_api_key = True
else:
    _has_api_key = False
    _model = None

# ------------------------------------------------------------------
# Heuristic fallbacks (used when no API key or on errors)
# ------------------------------------------------------------------

def _fallback_verify_work_order(
    problem_desc: str,
    work_done: str,
    materials_used: List[str],
) -> Dict[str, Any]:
    """Heuristic fallback for ``verify_work_order`` when Gemini is unavailable."""

    score = 70
    verdict = "Принято с замечаниями"
    explanation = (
        "Автоматическая оценка: ключ API Gemini не настроен, "
        "применены эвристические правила."
    )

    problem_lower = problem_desc.lower()
    work_lower = work_done.lower()
    materials_lower = [m.lower() for m in materials_used]

    # Simple keyword-driven adjustments
    if any(
        word in problem_lower for word in ["течь", "утечка", "воды", "залив"]
    ):
        if any("труба" in m or "тюбинк" in m for m in materials_lower):
            score = 85
            verdict = "Принято"
            explanation = "Устранена течка, материалы соответствуют работе."

    if any(word in problem_lower for word in ["обрыв", "разрыв"]):
        if any("арматура" in m or "винт" in m for m in materials_lower):
            score = 80
            if verdict != "Принято":
                verdict = "Принято с замечаниями"
            explanation = "Обрыв устранен, использованы подходящие материалы."

    # Clamp score to 0–100 and return structured dict
    score = max(0, min(100, score))
    return {
        "verdict": verdict,
        "score": score,
        "explanation": explanation,
    }


def _fallback_analyze_photos(
    image_before_bytes: bytes,
    image_after_bytes: bytes,
) -> Dict[str, Any]:
    """Heuristic fallback for ``analyze_photos_before_after`` when Gemini is unavailable."""

    return {
        "rating": 3,
        "verdict": "Анализ невозможен: ключ API Gemini не настроен.",
        "note": "Без доступа к модели невозможно оценить изменения на фото.",
    }


# ------------------------------------------------------------------
# Core function 1: verify_work_order
# ------------------------------------------------------------------


def verify_work_order(
    problem_desc: str,
    work_done: str,
    materials_used: List[str],
) -> Dict[str, Any]:
    """Verify the quality of a work order closure.

    Uses Google Gemini ``gemini-1.5-flash`` to evaluate:

    * quality of work-order closure,
    * logic of materials expenditure,
    * correspondence of performed works to the reported problem.

    Returns a strict JSON-like dict:

    .. code-block:: python

        {
            "verdict": "Принято"
                        | "Принято с замечаниями"
                        | "Требует доработки",
            "score":      integer 0..100,
            "explanation": "краткое экспертное пояснение для мастера и рабочего",
        }

    Protection: the response is parsed inside ``try/except``. If the model
    does not return valid JSON, a structured fallback (from
    ``_fallback_verify_work_order``) is returned instead of raising.

    Parameters
    ----------
    problem_desc : str
        Description of the problem reported on the work order.
    work_done : str
        Description of the works that were performed.
    materials_used : list[str]
        List of materials (ТМЦ) that were consumed/spent.

    Returns
    -------
    dict
        ``{verdict, score, explanation}`` as described above.
    """
    # If no API key configured, jump straight to heuristic fallback
    if not _has_api_key:
        return _fallback_verify_work_order(problem_desc, work_done, materials_used)

    try:
        prompt = (
            "Ты — эксперт по производству АО «Костанайские минералы». "
            "Оцени качество закрытия наряда по предоставленным данным. "
            "Вердикт должен быть строгим JSON-объектом без дополнительного текста.\n"
            f"Описание проблемы: {problem_desc}\n"
            f"Описание выполненных работ: {work_done}\n"
            f"Списанные материалы (ТМЦ): {', '.join(materials_used)}\n"
            "\n"
            "Ответ оформи в следующем JSON-формате (только JSON, без ``` и прочего):\n"
            "{\n"
            '  "verdict": "Принято" | "Принято с замечаниями" | "Требует доработки",\n'
            '  "score": <целое число от 0 до 100>,\n'
            '  "explanation": "краткое экспертное пояснение для мастера и рабочего"\n'
            "}"
        )

        response = _model.generate_content(prompt)
        text = response.text.strip()

        # Strip potential Markdown fencing ```json ... ```
        if text.startswith("```"):
            parts = text.split("```", 2)
            if len(parts) >= 3:
                text = parts[1].strip()
            else:
                # If only one fence, strip first line
                text = "\n".join(text.split("\n")[1:]).strip()

        # Attempt to parse the whole text as JSON
        result: Any = None
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            # Try extracting a JSON block delimited by { and }
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                json_candidate = text[start : end + 1]
                try:
                    result = json.loads(json_candidate)
                except json.JSONDecodeError:
                    result = None
            # else result stays None

        # Validate that we have the required keys and they are correct types
        if result and all(k in result for k in ("verdict", "score", "explanation")):
            valid_verdicts = (
                "Принято",
                "Принято с замечаниями",
                "Требует доработки",
            )
            if result["verdict"] not in valid_verdicts:
                raise ValueError(
                    f"Invalid verdict value received: {result['verdict']!r}"
                )
            try:
                score_val = int(result["score"])
                if score_val < 0 or score_val > 100:
                    raise ValueError("Score out of allowed range 0–100")
                result["score"] = score_val
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Invalid score value: {result['score']!r}") from exc
            return result

        # Fallthrough: missing fields or unparseable → use fallback
        logger.warning(
            "Gemini verify_work_order response missing required fields, "
            "using heuristic fallback."
        )
        return _fallback_verify_work_order(problem_desc, work_done, materials_used)

    except Exception as exc:
        logger.error(
            f"Unexpected error in verify_work_order (Gemini call): {exc}"
        )
        return _fallback_verify_work_order(problem_desc, work_done, materials_used)


# ------------------------------------------------------------------
# Core function 2: analyze_photos_before_after
# ------------------------------------------------------------------


def analyze_photos_before_after(
    image_before_bytes: bytes,
    image_after_bytes: bytes,
) -> Dict[str, Any]:
    """Analyze before/after photos to check whether a fault was fixed.

    The Gemini multimodal model examines both images and checks:

    * whether the fault (leak, break, deformation) is eliminated,
    * whether any trash or foreign objects are left,
    * whether protective covers / caps are secured.

    Returns a dict with:

    .. code-block:: python

        {
            "rating": integer 1..5,       # 1 = worst, 5 = best
            "verdict":  "short textual verdict",
        }

    If the Gemini call fails or the response cannot be parsed, a heuristic
    fallback is returned so the caller's application does not crash.

    Parameters
    ----------
    image_before_bytes : bytes
        Photo of the component / area *before* the work was performed.
    image_after_bytes : bytes
        Photo of the component / area *after* the work was performed.

    Returns
    -------
    dict
        ``{rating, verdict}`` as described above.
    """
    # If no API key, use heuristic fallback immediately
    if not _has_api_key:
        return _fallback_analyze_photos(image_before_bytes, image_after_bytes)

    try:
        # Build the multimodal content block.
        # The library accepts dicts with ``mime_type`` and ``data`` (bytes).
        content = [
            {"mime_type": "image/jpeg", "data": image_before_bytes},
            {"mime_type": "image/jpeg", "data": image_after_bytes},
            {
                "text": (
                    "Проанализируй две фотографии: 'До' и 'После' ремонта/работы. "
                    "Проверь, устранена ли поломка (течь, обрыв, деформация). "
                    "Нет ли мусора на месте работы. "
                    "Закреплены ли защитные кожухи/крышки? "
                    "Вердикт оформи в JSON с полями: rating (1-5) и verdict (короткая строка). "
                    "Пример правильного ответа: {\"rating\": 5, \"verdict\": \"Поломка устранена, всё в порядке\"}"
                )
            },
        ]

        response = _model.generate_content(content)
        text = response.text.strip()

        # Attempt to pull out JSON from the model's reply
        result: Any = None
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract a JSON block embedded in surrounding text
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                json_candidate = text[start : end + 1]
                try:
                    result = json.loads(json_candidate)
                except json.JSONDecodeError:
                    result = None
            # else result stays None

        # Validate result structure
        if result and "rating" in result and "verdict" in result:
            # Normalise rating to integer 1..5
            try:
                rating = int(result["rating"])
                if rating < 1 or rating > 5:
                    rating = 3  # out-of-range → neutral default
                result["rating"] = rating
            except (ValueError, TypeError):
                rating = 3
                result["rating"] = rating
            return result

        # Fallback when parsing did not succeed
        logger.warning(
            "Gemini analyze_photos response missing required fields, "
            "using heuristic fallback."
        )
        return _fallback_analyze_photos(image_before_bytes, image_after_bytes)

    except Exception as exc:
        logger.error(
            f"Unexpected error in analyze_photos_before_after (Gemini call): {exc}"
        )
        return _fallback_analyze_photos(image_before_bytes, image_after_bytes)