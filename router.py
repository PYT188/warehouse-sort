#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分拣去向推理引擎。

两种模式：
1. 本地规则引擎：内置品类知识库，毫秒级、离线可用（默认）。
2. 大模型推理：检测到环境变量 OPENAI_API_KEY / DEEPSEEK_API_KEY 时自动启用，
   调用 OpenAI 兼容接口（默认 DeepSeek），失败或超时自动回退规则引擎。
"""

from __future__ import annotations

import json
import os
import re
import threading

import requests

# --------------------------------------------------------------------------- #
# 分拣区域定义（格口 -> 区域）
# --------------------------------------------------------------------------- #
ZONES = {
    "A": {"zone": "生鲜冷链区", "color": "#2dd4bf",
          "handling": "冷链 2–8℃，30 分钟内转入冷库，禁止与常温货混装"},
    "B": {"zone": "食品饮料区", "color": "#fbbf24",
          "handling": "常温避光，液体直立放置，重物在下轻物在上"},
    "C": {"zone": "3C 电子区", "color": "#60a5fa",
          "handling": "防静电袋单独包装，防摔防压，禁与液体同框"},
    "D": {"zone": "服饰箱包区", "color": "#c084fc",
          "handling": "防潮防压，挂装或平铺，箱包内填充定型"},
    "E": {"zone": "家居日用区", "color": "#fb923c",
          "handling": "大件叉车搬运，易碎品加护角，堆高不超过 3 层"},
    "F": {"zone": "文体办公区", "color": "#a3e635",
          "handling": "轻拿轻放，按品类分格，尖锐物品单独包裹"},
    "X": {"zone": "人工复核区", "color": "#f87171",
          "handling": "停止自动分拣，转人工复核，禁止进入自动格口"},
}

# --------------------------------------------------------------------------- #
# COCO 80 类：中文名 + 格口
# --------------------------------------------------------------------------- #
ITEMS: dict[str, tuple[str, str]] = {
    # 人 / 动物 / 交通工具 -> 人工复核
    "person": ("人员", "X"), "bird": ("鸟", "X"), "cat": ("猫", "X"), "dog": ("狗", "X"),
    "horse": ("马", "X"), "sheep": ("羊", "X"), "cow": ("牛", "X"), "elephant": ("大象", "X"),
    "bear": ("熊", "X"), "zebra": ("斑马", "X"), "giraffe": ("长颈鹿", "X"),
    "bicycle": ("自行车", "X"), "car": ("汽车", "X"), "motorcycle": ("摩托车", "X"),
    "airplane": ("飞机", "X"), "bus": ("公交车", "X"), "train": ("火车", "X"),
    "truck": ("卡车", "X"), "boat": ("船", "X"), "traffic light": ("红绿灯", "X"),
    "fire hydrant": ("消防栓", "X"), "stop sign": ("停止标志", "X"),
    "parking meter": ("停车计时器", "X"),

    # A 生鲜冷链
    "banana": ("香蕉", "A"), "apple": ("苹果", "A"), "orange": ("橙子", "A"),
    "broccoli": ("西兰花", "A"), "carrot": ("胡萝卜", "A"), "sandwich": ("三明治", "A"),
    "hot dog": ("热狗", "A"), "pizza": ("披萨", "A"), "donut": ("甜甜圈", "A"),
    "cake": ("蛋糕", "A"),

    # B 食品饮料
    "bottle": ("瓶子", "B"), "wine glass": ("酒杯", "B"), "cup": ("杯子", "B"),
    "fork": ("叉子", "B"), "knife": ("刀", "B"), "spoon": ("勺子", "B"),
    "bowl": ("碗", "B"), "microwave": ("微波炉", "B"), "oven": ("烤箱", "B"),
    "toaster": ("烤面包机", "B"), "refrigerator": ("冰箱", "B"),

    # C 3C 电子
    "tv": ("电视", "C"), "laptop": ("笔记本", "C"), "mouse": ("鼠标", "C"),
    "remote": ("遥控器", "C"), "keyboard": ("键盘", "C"), "cell phone": ("手机", "C"),
    "hair drier": ("吹风机", "C"),

    # D 服饰箱包
    "backpack": ("背包", "D"), "umbrella": ("雨伞", "D"), "handbag": ("手提包", "D"),
    "tie": ("领带", "D"), "suitcase": ("行李箱", "D"),

    # E 家居日用
    "chair": ("椅子", "E"), "couch": ("沙发", "E"), "bed": ("床", "E"),
    "dining table": ("餐桌", "E"), "bench": ("长椅", "E"), "toilet": ("马桶", "E"),
    "sink": ("水槽", "E"), "potted plant": ("盆栽", "E"), "vase": ("花瓶", "E"),
    "teddy bear": ("泰迪熊", "E"),

    # F 文体办公
    "book": ("书", "F"), "clock": ("时钟", "F"), "scissors": ("剪刀", "F"),
    "frisbee": ("飞盘", "F"), "skis": ("滑雪板", "F"), "snowboard": ("单板", "F"),
    "sports ball": ("球", "F"), "kite": ("风筝", "F"), "baseball bat": ("棒球棒", "F"),
    "baseball glove": ("棒球手套", "F"), "skateboard": ("滑板", "F"),
    "surfboard": ("冲浪板", "F"), "tennis racket": ("网球拍", "F"),
    "toothbrush": ("牙刷", "F"),
}


# --------------------------------------------------------------------------- #
# 本地规则引擎
# --------------------------------------------------------------------------- #
def route_by_rule(class_name: str) -> dict:
    key = class_name.strip().lower()
    zh, chute = ITEMS.get(key, (class_name, "X"))
    zone = ZONES[chute]
    if key in ITEMS:
        if chute == "X":
            reason = f"{zh}属于非货品/异常目标，不进入自动格口，转 X 号人工复核区判定去向。"
        else:
            reason = f"{zh}属于「{zone['zone']}」品类，按既定分拨规则送往 {chute} 号格口。"
    else:
        reason = f"知识库中没有「{class_name}」的品类记录，无法自动定级，转人工复核。"
    return {
        "item": key,
        "item_zh": zh,
        "chute": chute,
        "zone": zone["zone"],
        "color": zone["color"],
        "handling": zone["handling"],
        "reason": reason,
        "engine": "rule",
    }


# --------------------------------------------------------------------------- #
# 大模型推理（可选）
# --------------------------------------------------------------------------- #
class LLMClient:
    """OpenAI 兼容接口客户端（默认指向 DeepSeek）。"""

    def __init__(self) -> None:
        self.key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        self.base = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")
        self.enabled = bool(self.key)

    def route(self, class_name: str, item_zh: str, timeout: float = 6.0) -> dict | None:
        if not self.enabled:
            return None
        prompt = (
            "你是仓储分拣系统的调度大脑。仓库有 7 个格口：\n"
            + "\n".join(f"{k} {v['zone']}：{v['handling']}" for k, v in ZONES.items())
            + f"\n\n现在摄像头识别到物品：{item_zh}（英文标签 {class_name}）。\n"
            "请判断它的最终去向，只输出 JSON，不要解释，格式：\n"
            '{"chute":"A-F 或 X","reason":"30 字以内的分拨理由","handling":"一句操作要求"}'
        )
        try:
            resp = requests.post(
                f"{self.base}/chat/completions",
                headers={"Authorization": f"Bearer {self.key}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": 300,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            data = _extract_json(text)
            chute = str(data.get("chute", "")).upper().strip()
            if chute not in ZONES:
                return None
            zone = ZONES[chute]
            return {
                "item": class_name,
                "item_zh": item_zh,
                "chute": chute,
                "zone": zone["zone"],
                "color": zone["color"],
                "handling": data.get("handling") or zone["handling"],
                "reason": data.get("reason") or f"大模型判定送往 {chute} 号格口。",
                "engine": "llm",
            }
        except Exception:
            return None


def _extract_json(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象。"""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("no json")
    return json.loads(match.group(0))


# --------------------------------------------------------------------------- #
# 统一入口（带缓存 + 自动回退）
# --------------------------------------------------------------------------- #
class Router:
    def __init__(self) -> None:
        self.llm = LLMClient()
        self.cache: dict[str, dict] = {}
        self.lock = threading.Lock()

    @property
    def engine_name(self) -> str:
        return f"大模型 {self.llm.model}" if self.llm.enabled else "本地规则引擎"

    def route(self, class_name: str) -> dict:
        key = class_name.strip().lower()
        with self.lock:
            if key in self.cache:
                return self.cache[key]

        result = None
        if self.llm.enabled:
            zh = ITEMS.get(key, (class_name, "X"))[0]
            result = self.llm.route(key, zh)
        if result is None:
            result = route_by_rule(key)

        with self.lock:
            self.cache[key] = result
        return result


if __name__ == "__main__":
    r = Router()
    print("engine:", r.engine_name)
    for name in ("bottle", "laptop", "banana", "person", "unknown_thing"):
        out = r.route(name)
        print(f"{name:14s} -> {out['chute']} {out['zone']:8s} | {out['reason']}")
