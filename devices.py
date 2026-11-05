#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备接入层：把分拣结果下发给 PLC / 机器人 / 网关。

支持四种后端，用环境变量切换：
    DEVICE=mock    只记录，不接硬件（默认，用于联调前端）
    DEVICE=tcp     TCP + JSON 行协议，对接自研网关/机械臂
    DEVICE=modbus  Modbus TCP，对接 PLC（西门子/汇川/台达等大多支持）
    DEVICE=mqtt    MQTT，对接 SCADA / 云端 / 边缘网关
    DEVICE=off     完全关闭下发

安全原则：视觉只“建议”去向，真正的动作由 PLC 执行；每条指令都带幂等 ID 和超时。
"""

from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
import uuid

# 格口 -> PLC 里约定的整型码（A=1 ... F=6, X=7）
CHUTE_CODE = {code: i + 1 for i, code in enumerate("ABCDEFX")}


# --------------------------------------------------------------------------- #
# 各后端
# --------------------------------------------------------------------------- #
class MockDevice:
    kind = "mock"

    def __init__(self) -> None:
        self.sent = []

    def send(self, chute: str, item: str, conf: float, cmd_id: str) -> dict:
        self.sent.append({"chute": chute, "item": item, "conf": conf, "id": cmd_id})
        return {"ok": True, "detail": "mock"}

    def close(self) -> None:
        pass


class TcpJsonDevice:
    """一行一个 JSON，发完等一行 {"ack":"ok"}。"""

    kind = "tcp"

    def __init__(self, host: str, port: int, timeout: float = 0.6) -> None:
        self.addr = (host, port)
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()

    def _connect(self) -> None:
        sock = socket.create_connection(self.addr, timeout=self.timeout)
        sock.settimeout(self.timeout)
        self.sock = sock

    def send(self, chute: str, item: str, conf: float, cmd_id: str) -> dict:
        payload = {
            "cmd": "sort",
            "id": cmd_id,
            "chute": chute,
            "code": CHUTE_CODE.get(chute, 7),
            "item": item,
            "conf": round(conf, 3),
            "ts": round(time.time(), 3),
        }
        with self.lock:
            try:
                if self.sock is None:
                    self._connect()
                self.sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    chunk = self.sock.recv(1024)
                    if not chunk:
                        raise ConnectionError("closed by peer")
                    buf += chunk
                reply = json.loads(buf.split(b"\n", 1)[0])
                return {"ok": reply.get("ack") == "ok", "detail": reply.get("msg", "")}
            except Exception as exc:
                self.close()
                return {"ok": False, "detail": f"tcp: {exc}"}

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


class ModbusTcpDevice:
    """写格口号到保持寄存器，再轮询状态寄存器等 PLC 确认。

    寄存器约定（可用环境变量改）：
        HR[0] 格口号（1-7）
        HR[1] 状态：1 = 已接受，2 = 被拒绝/满料
    """

    kind = "modbus"

    def __init__(self, host: str, port: int, unit: int = 1,
                 reg_chute: int = 0, reg_status: int = 1,
                 timeout: float = 0.5) -> None:
        from pymodbus.client import ModbusTcpClient

        self.unit = unit
        self.reg_chute = reg_chute
        self.reg_status = reg_status
        self.timeout = timeout
        self.client = ModbusTcpClient(host, port=port, timeout=timeout)

    def send(self, chute: str, item: str, conf: float, cmd_id: str) -> dict:
        code = CHUTE_CODE.get(chute, 7)
        try:
            if not self.client.connected:
                self.client.connect()
            self.client.write_register(self.reg_chute, code, device_id=self.unit)

            deadline = time.time() + max(self.timeout * 6, 1.0)
            status = 0
            while time.time() < deadline:
                rr = self.client.read_holding_registers(self.reg_status, count=1,
                                                        device_id=self.unit)
                if rr.isError():
                    return {"ok": False, "detail": f"read error: {rr}"}
                status = rr.registers[0]
                if status == 1:
                    return {"ok": True, "detail": f"code={code} ack"}
                if status == 2:
                    return {"ok": False, "detail": f"code={code} rejected"}
                time.sleep(0.02)
            return {"ok": False, "detail": f"code={code} timeout status={status}"}
        except Exception as exc:
            return {"ok": False, "detail": f"modbus: {exc}"}

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


class MqttDevice:
    kind = "mqtt"

    def __init__(self, host: str, port: int, topic: str, timeout: float = 0.6) -> None:
        import paho.mqtt.client as mqtt

        self.topic = topic
        self.timeout = timeout
        self.client = mqtt.Client()
        self.client.connect(host, port, keepalive=30)
        self.client.loop_start()

    def send(self, chute: str, item: str, conf: float, cmd_id: str) -> dict:
        payload = {"cmd": "sort", "id": cmd_id, "chute": chute,
                   "code": CHUTE_CODE.get(chute, 7), "item": item,
                   "conf": round(conf, 3), "ts": round(time.time(), 3)}
        try:
            info = self.client.publish(self.topic, json.dumps(payload, ensure_ascii=False), qos=1)
            info.wait_for_publish(timeout=self.timeout)
            return {"ok": True, "detail": f"topic={self.topic}"}
        except Exception as exc:
            return {"ok": False, "detail": f"mqtt: {exc}"}

    def close(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 工厂函数
# --------------------------------------------------------------------------- #
def make_device():
    kind = os.getenv("DEVICE", "mock").strip().lower()
    if kind in ("", "off", "none"):
        return None
    if kind == "mock":
        return MockDevice()
    host = os.getenv("DEVICE_HOST", "127.0.0.1")
    port = int(os.getenv("DEVICE_PORT", "5020"))
    timeout = float(os.getenv("DEVICE_TIMEOUT", "0.6"))
    if kind == "tcp":
        return TcpJsonDevice(host, port, timeout)
    if kind == "modbus":
        return ModbusTcpDevice(
            host, port,
            unit=int(os.getenv("MODBUS_UNIT", "1")),
            reg_chute=int(os.getenv("MODBUS_REG_CHUTE", "0")),
            reg_status=int(os.getenv("MODBUS_REG_STATUS", "1")),
            timeout=timeout,
        )
    if kind == "mqtt":
        return MqttDevice(host, port, os.getenv("MQTT_TOPIC", "sort/cmd"), timeout)
    raise SystemExit(f"未知 DEVICE={kind}，可选 mock / tcp / modbus / mqtt / off")


# --------------------------------------------------------------------------- #
# 异步下发队列（串行执行，避免指令打架）
# --------------------------------------------------------------------------- #
class Dispatcher:
    def __init__(self, device) -> None:
        self.device = device
        self.q: queue.Queue = queue.Queue(maxsize=32)
        self.lock = threading.Lock()
        self.status = {
            "enabled": device is not None,
            "kind": getattr(device, "kind", "off"),
            "sent": 0,
            "ok": 0,
            "fail": 0,
            "last": None,
            "backlog": 0,
        }
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, chute: str, item: str, conf: float) -> bool:
        job = {"chute": chute, "item": item, "conf": conf, "id": uuid.uuid4().hex[:10]}
        try:
            self.q.put_nowait(job)
        except queue.Full:
            return False
        with self.lock:
            self.status["backlog"] = self.q.qsize()
        return True

    def _run(self) -> None:
        while True:
            job = self.q.get()
            t0 = time.perf_counter()
            if self.device is None:
                res = {"ok": False, "detail": "device off"}
            else:
                try:
                    res = self.device.send(job["chute"], job["item"], job["conf"], job["id"])
                except Exception as exc:
                    res = {"ok": False, "detail": f"send error: {exc}"}
            cost = (time.perf_counter() - t0) * 1000
            with self.lock:
                self.status["sent"] += 1
                self.status["ok" if res["ok"] else "fail"] += 1
                self.status["backlog"] = self.q.qsize()
                self.status["last"] = {
                    "chute": job["chute"],
                    "item": job["item"],
                    "conf": round(job["conf"], 3),
                    "id": job["id"],
                    "ok": res["ok"],
                    "detail": res.get("detail", ""),
                    "ms": round(cost, 1),
                    "ts": time.strftime("%H:%M:%S"),
                }

    def get_status(self) -> dict:
        with self.lock:
            data = dict(self.status)
            data["last"] = dict(data["last"]) if data["last"] else None
            return data
