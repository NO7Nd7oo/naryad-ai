import sys
import os

sys_path = r"C:\Users\NITRO\hackathon-starter\src"
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from src.ai_engine import (
    _has_api_key,
    _fallback_verify_work_order,
    _fallback_analyze_photos,
    verify_work_order,
    analyze_photos_before_after,
)

print("_has_api_key:", _has_api_key)

# Test fallback verify_work_order
result = _fallback_verify_work_order(
    problem_desc="Течь в радиаторе",
    work_done="Заменен радиатор, подключены шланги",
    materials_used=["медьной трубопровод", "новый радиатор"],
)
print("Fallback verify_work_order result:", result)

# Test fallback analyze_photos
result2 = _fallback_analyze_photos(b"fake_before", b"fake_after")
print("Fallback analyze_photos result:", result2)

print("\nAll basic tests passed!")