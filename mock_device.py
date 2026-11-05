#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟 PLC / 网关，用来在没有真实设备时联调。

用法：
    python mock_device.py                 # TCP + JSON，端口 5020
    python mock_device.py --mode modbus   # Modbus TCP，端口 5020
    python mock_device.py --fail-rate 0.2 # 20% 概率回复“格口满/拒绝”
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import threading
import time

CHUTE_NAME = {1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F", 7: "X"}


def recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


# --------------------------------------------------------------------------- #
# TCP + JSON
# --------------------------------------------------------------------------- #
def handle_json(conn: socket.socket, fail_rate: float) -> None:
    f = conn.makefile("rwb")
    while True:
        line = f.readline()
        if not line:
            break
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            f.write(b'{"ack":"error","msg":"bad json"}\n')
            f.flush()
            continue
        ok = random.random() >= fail_rate
        print(f"[PLC] 格口={cmd.get('chute')} 码={cmd.get('code')} "
              f"物品={cmd.get('item')} 置信度={cmd.get('conf')} id={cmd.get('id')} "
              f"-> {'接受' if ok else '拒绝'}", flush=True)
        reply = {"ack": "ok" if ok else "reject", "msg": "" if ok else "chute full"}
        f.write((json.dumps(reply) + "\n").encode())
        f.flush()


def serve_tcp(host: str, port: int, fail_rate: float) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    print(f"[mock] TCP+JSON 监听 {host}:{port}", flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_json, args=(conn, fail_rate), daemon=True).start()


# --------------------------------------------------------------------------- #
# Modbus TCP（FC3 读保持寄存器 / FC6 写单个寄存器）
# --------------------------------------------------------------------------- #
class ModbusSim:
    def __init__(self, host: str, port: int, fail_rate: float) -> None:
        self.host = host
        self.port = port
        self.fail_rate = fail_rate
        self.regs = [0] * 64  # regs[0]=格口号, regs[1]=状态(1 接受 / 2 拒绝)

    def serve(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(8)
        print(f"[mock] Modbus TCP 监听 {self.host}:{self.port} "
              f"(HR0=格口号, HR1=状态)", flush=True)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            while True:
                mbap = recv_exact(conn, 7)
                tid, pid, length, uid = (mbap[0:2], mbap[2:4], int.from_bytes(mbap[4:6], "big"), mbap[6:7])
                pdu = recv_exact(conn, length - 1)
                fc = pdu[0]
                if fc == 6:  # write single register
                    addr = int.from_bytes(pdu[1:3], "big")
                    value = int.from_bytes(pdu[3:5], "big")
                    self.regs[addr] = value
                    self._on_write(addr, value)
                    resp = pdu
                elif fc == 3:  # read holding registers
                    addr = int.from_bytes(pdu[1:3], "big")
                    count = int.from_bytes(pdu[3:5], "big")
                    vals = b"".join(int(self.regs[addr + i]).to_bytes(2, "big") for i in range(count))
                    resp = bytes([3, len(vals)]) + vals
                else:
                    resp = bytes([fc | 0x80, 1])
                body = uid + resp
                conn.sendall(tid + pid + len(body).to_bytes(2, "big") + body)
        except Exception as exc:
            print(f"[mock] 连接异常: {exc!r}", flush=True)
        finally:
            conn.close()

    def _on_write(self, addr: int, value: int) -> None:
        if addr != 0:
            return
        ok = random.random() >= self.fail_rate
        print(f"[PLC] 写入 HR0={value} -> 格口 {CHUTE_NAME.get(value, '?')} "
              f"{'接受' if ok else '拒绝'}", flush=True)
        self.regs[1] = 1 if ok else 2
        time.sleep(0.02)  # 模拟 PLC 处理时间


def main() -> None:
    p = argparse.ArgumentParser(description="模拟 PLC / 网关")
    p.add_argument("--mode", choices=["tcp", "modbus"], default="tcp")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5020)
    p.add_argument("--fail-rate", type=float, default=0.0)
    args = p.parse_args()

    if args.mode == "modbus":
        ModbusSim(args.host, args.port, args.fail_rate).serve()
    else:
        serve_tcp(args.host, args.port, args.fail_rate)


if __name__ == "__main__":
    main()
