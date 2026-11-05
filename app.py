#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能分拣中枢 —— 视觉识别 + 去向推理 + Web 前端。

启动：
    python app.py            # 打开 http://127.0.0.1:8765
"""

from __future__ import annotations

import os
import sys
import threading
import time

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template, request

from router import ZONES, Router
from devices import Dispatcher, make_device

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = None
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv("PORT", "8765"))
WIDTH = int(os.getenv("CAM_WIDTH", "1280"))
HEIGHT = int(os.getenv("CAM_HEIGHT", "720"))

app = Flask(__name__)


# --------------------------------------------------------------------------- #
# 摄像头
# --------------------------------------------------------------------------- #
class Camera:
    """后台线程持续取流，只保留最新一帧。"""

    def __init__(self, index: int = 0, width: int = WIDTH, height: int = HEIGHT):
        self.cap = None
        self.backend = "offline"
        self.latest = None
        for name, api in (("MSMF", cv2.CAP_MSMF), ("DSHOW", cv2.CAP_DSHOW)):
            cap = cv2.VideoCapture(index, api)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                cap.set(cv2.CAP_PROP_FPS, 30)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                ok, frame = cap.read()
                if ok and frame is not None:
                    self.cap = cap
                    self.backend = name
                    self.latest = frame
                    break
                cap.release()

    def read(self):
        """同步取一帧（与推理同线程，实测比双线程快一倍）。"""
        if self.cap is None:
            return None
        ok, frame = self.cap.read()
        return frame if ok else None

    def stop(self) -> None:
        if self.cap is not None:
            self.cap.release()


def offline_frame(text: str = "CAMERA OFFLINE") -> np.ndarray:
    frame = np.full((HEIGHT, WIDTH, 3), 24, dtype=np.uint8)
    cv2.putText(frame, text, (WIDTH // 2 - 160, HEIGHT // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 1.4, (90, 90, 90), 3, cv2.LINE_AA)
    return frame


# --------------------------------------------------------------------------- #
# 推理 + 绘制线程
# --------------------------------------------------------------------------- #
class Pipeline:
    def __init__(self, router: Router, conf: float = 0.35, imgsz: int = 640):
        from ultralytics import YOLO
        import torch

        self.router = router
        self.conf = conf
        self.imgsz = imgsz
        model_path = ROOT / "yolo11s.pt"
        self.model = YOLO(str(model_path) if model_path.exists() else "yolo11s.pt")
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        self.precision = 16 if self.device.type == "cuda" else 32
        self.names = self.model.names

        self.camera = Camera()
        self.lock = threading.Lock()
        self.jpeg: bytes | None = None
        self.state = {
            "items": [],
            "fps": 0.0,
            "camera": self.camera.cap is not None,
            "backend": self.camera.backend,
            "engine": router.engine_name,
            "totals": {k: 0 for k in ZONES},
            "seen": 0,
            "ts": time.time(),
        }
        self._last_seen: dict[str, float] = {}
        self._fps = 0.0
        self._last_read = 0.0
        self.dispatcher = Dispatcher(make_device())
        self._last_dispatch: dict[str, float] = {}
        self.dispatch_cooldown = float(os.getenv("DISPATCH_COOLDOWN", "2.0"))
        self.dispatch_conf = float(os.getenv("DISPATCH_CONF", "0.5"))
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # ---- 主循环 ----
    def _loop(self) -> None:
        while self.running:
            t_wait = time.perf_counter()
            frame = self.camera.read()
            now_read = time.perf_counter()
            if frame is None:
                frame = offline_frame()
                time.sleep(0.1)
            elif self._last_read:
                dt = now_read - self._last_read
                if dt > 0:
                    self._fps = 0.9 * self._fps + 0.1 * (1.0 / max(dt, 1 / 120.0))
            self._last_read = now_read
            t0 = time.perf_counter()
            try:
                results = self.model.predict(
                    frame, conf=self.conf, iou=0.45, imgsz=self.imgsz,
                    verbose=False, quantize=self.precision, device=str(self.device),
                )
            except Exception as exc:  # 推理异常不应杀死整个服务
                print(f"[warn] predict failed: {exc}")
                time.sleep(0.2)
                continue
            infer_ms = (time.perf_counter() - t0) * 1000.0

            r = results[0]
            items = []
            for box in r.boxes:
                cls_id = int(box.cls)
                name = str(self.names[cls_id])
                conf = float(box.conf)
                route = self.router.route(name)
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
                items.append({
                    "item": name,
                    "item_zh": route["item_zh"],
                    "conf": round(conf, 3),
                    "chute": route["chute"],
                    "zone": route["zone"],
                    "color": route["color"],
                    "handling": route["handling"],
                    "reason": route["reason"],
                    "engine": route["engine"],
                    "box": [x1, y1, x2, y2],
                })
            items.sort(key=lambda it: it["conf"], reverse=True)
            self._maybe_dispatch(items)

            t_draw = time.perf_counter()
            annotated = draw(frame, items)
            ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 82])
            t_enc = time.perf_counter()

            if os.getenv("SORT_DEBUG"):
                self._dbg = getattr(self, "_dbg", 0) + 1
                if self._dbg % 30 == 0:
                    print(f"[dbg] wait={(t0 - t_wait) * 1000:.1f} "
                          f"infer={infer_ms:.1f} draw+enc={(t_enc - t_draw) * 1000:.1f} "
                          f"total={(t_enc - t_wait) * 1000:.1f} ms")

            # 统计：同一品类 3 秒内只计一次，避免刷屏
            now = time.time()
            with self.lock:
                if ok:
                    self.jpeg = buf.tobytes()
                totals = self.state["totals"]
                for it in items:
                    last = self._last_seen.get(it["item"], 0)
                    if now - last > 3.0:
                        totals[it["chute"]] = totals.get(it["chute"], 0) + 1
                        self.state["seen"] += 1
                        self._last_seen[it["item"]] = now
                self.state.update({
                    "items": items,
                    "fps": round(self._fps, 1),
                    "infer_ms": round(infer_ms, 1),
                    "camera": self.camera.cap is not None,
                    "device": self.dispatcher.get_status(),
                    "ts": now,
                })

    def _maybe_dispatch(self, items: list[dict]) -> None:
        """只把置信度最高、且没在冷却期内的目标下发给 PLC。"""
        now = time.time()
        for it in items:
            if it["conf"] < self.dispatch_conf:
                continue
            if now - self._last_dispatch.get(it["item"], 0) < self.dispatch_cooldown:
                continue
            if self.dispatcher.submit(it["chute"], it["item"], it["conf"]):
                self._last_dispatch[it["item"]] = now
            break

    def snapshot(self) -> bytes | None:
        with self.lock:
            return self.jpeg

    def get_state(self) -> dict:
        with self.lock:
            return dict(self.state)

    def stop(self) -> None:
        self.running = False
        self.camera.stop()


# --------------------------------------------------------------------------- #
# 绘制
# --------------------------------------------------------------------------- #
def _hex_to_bgr(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return b, g, r


def draw(frame: np.ndarray, items: list[dict]) -> np.ndarray:
    out = frame.copy()
    for it in items:
        x1, y1, x2, y2 = it["box"]
        color = _hex_to_bgr(it["color"])
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = f'{it["item"]} {it["conf"]:.2f} -> {it["chute"]}'
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        cv2.rectangle(out, (x1, max(0, y1 - th - 10)), (x1 + tw + 10, y1), color, -1)
        cv2.putText(out, label, (x1 + 5, y1 - 6), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (16, 16, 16), 1, cv2.LINE_AA)
    return out


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
ROUTER = Router()
PIPE = Pipeline(ROUTER)


@app.route("/")
def index():
    return render_template("index.html", engine=ROUTER.engine_name, zones=ZONES)


@app.route("/video_feed")
def video_feed():
    def gen():
        while True:
            jpg = PIPE.snapshot()
            if jpg is None:
                time.sleep(0.05)
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
            time.sleep(0.02)

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/state")
def api_state():
    return jsonify(PIPE.get_state())


@app.route("/api/snapshot")
def api_snapshot():
    jpg = PIPE.snapshot()
    if jpg is None:
        return jsonify({"error": "no frame"}), 503
    return Response(jpg, mimetype="image/jpeg")


@app.route("/api/route")
def api_route():
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"error": "missing name"}), 400
    data = ROUTER.route(name)
    data["engine_name"] = ROUTER.engine_name
    return jsonify(data)


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "engine": ROUTER.engine_name,
                    "camera": PIPE.get_state()["camera"]})


if __name__ == "__main__":
    print(f"[engine] {ROUTER.engine_name}")
    print(f"[device] {PIPE.dispatcher.get_status()['kind']}")
    print(f"[camera] {PIPE.get_state()['backend']}  {WIDTH}x{HEIGHT}")
    print(f"[web   ] http://127.0.0.1:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)
