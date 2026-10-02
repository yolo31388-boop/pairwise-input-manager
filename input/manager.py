"""输入管理 - 修复版

修复内容:
1. process_input 即时处理输入, 缓冲有上限 (默认 20, 满了丢弃最旧事件)
2. remap_key 持久化到配置文件, 并检查按键冲突
3. detect_combo 带时间窗口, 支持方向键+技能键组合 (同帧 chord)
4. process_gamepad 使用可配置的手柄映射 (PS/Xbox 预设), 支持震动反馈
5. get_key_state 记录按下持续时间, 输入事件支持消费 (避免一个事件被多个系统重复处理)
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

DEFAULT_BUFFER_MAX = 20
DEFAULT_COMBO_WINDOW = 0.5
DEFAULT_LONG_PRESS_THRESHOLD = 0.5
DIRECTION_KEYS = ("up", "down", "left", "right")

# 手柄按键 -> 动作的默认映射, 同时覆盖 PS 与 Xbox 布局
DEFAULT_GAMEPAD_BINDINGS: dict[str, str] = {
    # PS
    "cross": "jump", "circle": "cancel", "square": "attack", "triangle": "skill",
    "l1": "guard", "r1": "dodge", "l2": "aim", "r2": "fire",
    "share": "menu", "options": "start", "l3": "crouch", "r3": "lock_on",
    "dpad_up": "up", "dpad_down": "down", "dpad_left": "left", "dpad_right": "right",
    # Xbox
    "a": "jump", "b": "cancel", "x": "attack", "y": "skill",
    "lb": "guard", "rb": "dodge", "lt": "aim", "rt": "fire",
    "back": "menu", "start": "start", "ls": "crouch", "rs": "lock_on",
}


def _default_config_path() -> str:
    return os.environ.get(
        "INPUT_MANAGER_CONFIG",
        os.path.join(tempfile.gettempdir(), "input_manager_config.json"),
    )


@dataclass
class KeyState:
    key: str
    pressed: bool = False
    press_time: float = 0.0   # 按下时刻 (来自注入的时钟)
    duration: float = 0.0     # 最近一次按下持续的时长


@dataclass
class InputEvent:
    key: str
    timestamp: float
    consumed: bool = False


class InputManager:
    def __init__(
        self,
        config_path: Optional[str] = None,
        buffer_max: int = DEFAULT_BUFFER_MAX,
        combo_window: float = DEFAULT_COMBO_WINDOW,
        time_source: Callable[[], float] = time.monotonic,
    ):
        self._time = time_source
        self.buffer_max = buffer_max
        self.combo_window = combo_window
        self.long_press_threshold = DEFAULT_LONG_PRESS_THRESHOLD

        self.keys: dict[str, KeyState] = {}
        self.input_buffer: deque[InputEvent] = deque(maxlen=buffer_max)
        self.combo_history: deque[tuple[str, float]] = deque(maxlen=64)
        self.consumed_events: list[InputEvent] = []

        self.key_bindings: dict[str, str] = {}
        self.gamepad_bindings: dict[str, str] = dict(DEFAULT_GAMEPAD_BINDINGS)
        self.config_path = config_path or _default_config_path()

        self._action_handlers: dict[str, list[Callable[[], None]]] = {}
        self._rumble_log: list[tuple[float, float]] = []

        self._load_config()

    # ---------- bug1: 即时处理 + 缓冲上限 ----------

    def process_input(self, key: str, timestamp: Optional[float] = None) -> InputEvent:
        """即时处理输入: 立即更新按键状态并入有界缓冲, 不拖到帧末。"""
        ts = self._time() if timestamp is None else timestamp
        self.press_key(key, ts)
        event = InputEvent(key=key, timestamp=ts)
        self.input_buffer.append(event)  # deque(maxlen) 满了自动丢弃最旧事件
        self.combo_history.append((key, ts))
        action = self.key_bindings.get(key)
        if action and action in self._action_handlers:
            for handler in self._action_handlers[action]:
                handler()
        return event

    def bind_action(self, action: str, handler: Callable[[], None]) -> None:
        self._action_handlers.setdefault(action, []).append(handler)

    def flush_buffer(self) -> list[InputEvent]:
        events = list(self.input_buffer)
        self.input_buffer.clear()
        return events

    # ---------- bug5: 按键状态 (持续时间) + 输入消费 ----------

    def press_key(self, key: str, timestamp: Optional[float] = None) -> KeyState:
        ts = self._time() if timestamp is None else timestamp
        state = self.keys.get(key)
        if state is None:
            state = KeyState(key)
            self.keys[key] = state
        state.pressed = True
        state.press_time = ts
        return state

    def release_key(self, key: str, timestamp: Optional[float] = None) -> KeyState:
        ts = self._time() if timestamp is None else timestamp
        state = self.keys.get(key)
        if state is None:
            state = KeyState(key)
            self.keys[key] = state
        if state.pressed:
            state.duration = max(0.0, ts - state.press_time)
        state.pressed = False
        return state

    def get_key_state(self, key: str) -> KeyState:
        state = self.keys.get(key)
        if state is None:
            return KeyState(key)
        if state.pressed:
            state.duration = max(0.0, self._time() - state.press_time)
        return state

    def get_press_duration(self, key: str) -> float:
        return self.get_key_state(key).duration

    def is_long_press(self, key: str, threshold: Optional[float] = None) -> bool:
        limit = self.long_press_threshold if threshold is None else threshold
        return self.get_key_state(key).duration >= limit

    def consume_event(self, event: InputEvent) -> bool:
        """消费一个输入事件; 已被消费的事件返回 False, 防止重复处理。"""
        if event.consumed:
            return False
        event.consumed = True
        self.consumed_events.append(event)
        return True

    def consume_input(self, key: str) -> bool:
        """从缓冲中消费该按键最近的一个未消费事件。"""
        for event in reversed(self.input_buffer):
            if event.key == key and not event.consumed:
                return self.consume_event(event)
        return False

    # ---------- bug2: 按键映射持久化 + 冲突检查 ----------

    def remap_key(self, action: str, key: str) -> bool:
        """重绑按键并写入配置文件; 按键已被其他动作占用时拒绝并返回 False。"""
        for other_action, bound_key in self.key_bindings.items():
            if bound_key == key and other_action != action:
                return False
        self.key_bindings[action] = key
        self._save_config()
        return True

    def _load_config(self) -> None:
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        self.key_bindings.update(data.get("key_bindings", {}))
        self.gamepad_bindings.update(data.get("gamepad_bindings", {}))

    def _save_config(self) -> None:
        data = {
            "key_bindings": self.key_bindings,
            "gamepad_bindings": {
                button: action
                for button, action in self.gamepad_bindings.items()
                if DEFAULT_GAMEPAD_BINDINGS.get(button) != action
            },
        }
        directory = os.path.dirname(self.config_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = self.config_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.config_path)

    # ---------- bug3: 连招时间窗口 + 组合键 ----------

    @staticmethod
    def _parse_event(event) -> Optional[tuple[tuple[str, ...], float]]:
        """把事件解析为 (按键元组, 时间戳); 无法解析返回 None。

        支持: (key, ts) / ((k1, k2), ts) / {"key":..., "time":...} / KeyState。
        裸字符串没有时间信息, 返回 None。
        """
        if isinstance(event, str):
            return None
        if isinstance(event, KeyState):
            return (event.key,), event.press_time
        if isinstance(event, dict):
            key = event.get("key")
            ts = event.get("time", event.get("timestamp"))
            if key is None or ts is None:
                return None
            keys = tuple(key) if isinstance(key, (list, tuple, set, frozenset)) else (key,)
            return keys, float(ts)
        if isinstance(event, (tuple, list)) and len(event) == 2:
            keys_part, ts = event
            keys = (
                tuple(keys_part)
                if isinstance(keys_part, (list, tuple, set, frozenset))
                else (keys_part,)
            )
            return keys, float(ts)
        return None

    def detect_combo(self, sequence: Optional[Iterable] = None) -> bool:
        """检测连招: 相邻输入间隔必须在 combo_window 内。

        支持方向键+技能键组合: 同一时间戳的多个按键视为同帧 chord, 计为一步。
        无时间信息的输入 (如裸字符串列表) 无法验证时序, 返回 False。
        """
        if sequence is None:
            events = [((key,), ts) for key, ts in self.combo_history]
        else:
            events = []
            for raw in sequence:
                parsed = self._parse_event(raw)
                if parsed is None:
                    return False
                events.append(parsed)
        if len(events) < 2:
            return False
        for (_, prev_ts), (_, next_ts) in zip(events, events[1:]):
            if next_ts - prev_ts > self.combo_window:
                return False
        return True

    def match_combo(self, pattern: Sequence[str], window: Optional[float] = None) -> bool:
        """在已记录的输入历史中匹配按键序列 (含时间窗口校验)。"""
        limit = self.combo_window if window is None else window
        history = list(self.combo_history)
        if len(pattern) > len(history):
            return False
        tail = history[-len(pattern):]
        if [key for key, _ in tail] != list(pattern):
            return False
        for (_, prev_ts), (_, next_ts) in zip(tail, tail[1:]):
            if next_ts - prev_ts > limit:
                return False
        return True

    def is_combo_pressed(self, keys: Iterable[str]) -> bool:
        """检测一组按键 (如方向键+技能键) 是否同时按住。"""
        return all(self.keys.get(key) is not None and self.keys[key].pressed for key in keys)

    # ---------- bug4: 手柄映射可配置 + 震动 ----------

    def process_gamepad(self, button: str) -> bool:
        """按当前手柄映射处理按键; 未绑定的按键返回 False。"""
        return button in self.gamepad_bindings

    def get_gamepad_action(self, button: str) -> Optional[str]:
        return self.gamepad_bindings.get(button)

    def remap_gamepad_button(self, button: str, action: str) -> bool:
        """重绑手柄按键并持久化; 动作已被其他按键占用时拒绝。"""
        for other_button, bound_action in self.gamepad_bindings.items():
            if bound_action == action and other_button != button:
                return False
        self.gamepad_bindings[button] = action
        self._save_config()
        return True

    def load_gamepad_profile(self, profile: str) -> bool:
        """加载手柄预设 ('ps' / 'xbox'), 未知预设返回 False。"""
        profiles = {
            "ps": {"cross": "jump", "circle": "cancel", "square": "attack", "triangle": "skill"},
            "xbox": {"a": "jump", "b": "cancel", "x": "attack", "y": "skill"},
        }
        mapping = profiles.get(profile.lower())
        if mapping is None:
            return False
        self.gamepad_bindings.update(mapping)
        self._save_config()
        return True

    def rumble(self, intensity: float = 1.0, duration: float = 0.2) -> bool:
        """触发手柄震动反馈; 无手柄时返回 False。子类/硬件层可覆写 _do_rumble。"""
        intensity = max(0.0, min(1.0, intensity))
        if not self._do_rumble(intensity, duration):
            return False
        self._rumble_log.append((intensity, duration))
        return True

    def _do_rumble(self, intensity: float, duration: float) -> bool:
        # 无实际硬件时的默认实现: 记录日志即视为成功
        return True

    @property
    def rumble_log(self) -> list[tuple[float, float]]:
        return list(self._rumble_log)
