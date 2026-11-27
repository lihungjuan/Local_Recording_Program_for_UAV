from __future__ import annotations

import datetime as _dt
import time

import cv2
import numpy as np


def format_timestamp(when: float | _dt.datetime | None = None) -> str:
    if when is None:
        when = time.time()
    if isinstance(when, (int, float)):
        when = _dt.datetime.fromtimestamp(when)
    return when.strftime("%Y-%m-%d %H:%M:%S")


def draw_timestamp(
    frame_bgr: np.ndarray,
    when: float | _dt.datetime | None = None,
    enabled: bool = True,
) -> np.ndarray:
    """Return a copy with a surveillance-style timestamp at the top-right."""
    if not enabled or frame_bgr is None:
        return frame_bgr
    out = frame_bgr.copy()
    height, width = out.shape[:2]
    text = format_timestamp(when)
    scale = max(0.55, min(1.15, (height / 720.0) * 0.9))
    thickness = max(1, int(round(scale * 2)))
    (text_w, text_h), baseline = cv2.getTextSize(
        text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness
    )
    margin = max(8, int(round(width * 0.012)))
    x = max(0, width - text_w - margin)
    y = max(text_h + margin, margin + text_h)
    cv2.putText(
        out, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
        (0, 0, 0), thickness + 2, cv2.LINE_AA,
    )
    cv2.putText(
        out, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
        (255, 255, 255), thickness, cv2.LINE_AA,
    )
    return out
