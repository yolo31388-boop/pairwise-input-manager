"""输入管理。

修复内容：
1. process_input 即时处理 + 有上限输入缓冲（溢出丢弃最旧事件）。
2. remap_key 持久化到 JSON 配置文件，并检查按键冲突。
3. detect_combo 有时间窗口，且支持方向键 + 技能键组合。
4. process_gamepad 使用可配置映射（PS/Xbox 通用），并支持震动反馈。
5. get_key_state 记录按下时刻/持续时间，区分点按与长按。
6. 输入事件支持消费（consume），避免同一事件被多个系统重复处理。
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Iterable

# 输入缓冲上限：超过后丢弃最旧的事件，防止连按无限排队。
MAX_BUFFER_SIZE = 20
# 连招默认时间窗口（秒）。
DEFAULT_COMBO_WINDOW = 0.5
# 长按判定阈值（秒）。
DEFAULT_HOLD_THRESHOLD = 0.5

# 方向键归一化表（搓招输入统一成 up/down/left/right）。
DIRECTION_ALIASES = {
    "up": "up", "w": "up", "arrowup": "up", "dpad_up": "up",
    "down": "down", "s": "down", "arrowdown": "down", "dpad_down": "down",
    "left": "left", "a": "left", "arrowleft": "left", "dpad_left": "left",
    "right": "right", "d": "right", "arrowright": "right", "dpad_right": "right",
}

# 手柄按钮 -> 规范化动作。PS 与 Xbox 两种常见命名都能识别，
# 用户可通过 config / remap_gamepad_button 完全自定义。
DEFAULT_GAMEPAD_BINDINGS: dict[str, str] = {
    # Xbox 命名
    "a": "jump",
    "b": "dodge",
    "x": "attack",
    "y": "skill",
    "lb": "guard",
    "rb": "dash",
    "lt": "aim",
    "rt": "fire",
    "start": "menu",
    "back": "map",
    # PS 命名
    "cross": "jump",
    "circle": "dodge",
    "square": "attack",
    "triangle": "skill",
    "l1": "guard",
    "r1": "dash",
    "l2": "aim",
    "r2": "fire",
    "options": "menu",
    "share": "map",
}

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "input_config.json"


def _normalize(key: str) -> str:
    return str(key).strip().lower() if key is not None else ""


@dataclass
class InputEvent:
    """单次按键事件。consumed 后其他系统可跳过该事件。"""

    key: str
    pressed: bool = True
    timestamp: float = 0.0
    consumed: bool = False
    action: str | None = None

    def consume(self) -> None:
        self.consumed = True

    def is_consumed(self) -> bool:
        return self.consumed


@dataclass
class KeyState:
    """按键状态。记录按下时刻并可计算持续时间。"""

    key: str
    pressed: bool = False
    press_time: float = 0.0
    release_time: float = 0.0
    duration: float = 0.0
    last_event_time: float = 0.0

    def hold_duration(self, now: float | None = None) -> float:
        """当前/本次按下的持续时间（秒）。"""
        if self.pressed:
            return (time.monotonic() if now is None else now) - self.press_time
        return self.duration

    def is_held(self, threshold: float = DEFAULT_HOLD_THRESHOLD,
                now: float | None = None) -> bool:
        return self.pressed and self.hold_duration(now) >= threshold

    def is_tap(self, threshold: float = DEFAULT_HOLD_THRESHOLD) -> bool:
        return not self.pressed and 0.0 < self.duration < threshold


class InputManager:
    def __init__(self,
                 config_path: str | os.PathLike | None = None,
                 max_buffer_size: int = MAX_BUFFER_SIZE,
                 combo_window: float = DEFAULT_COMBO_WINDOW,
                 clock: Callable[[], float] | None = None):
        self.max_buffer_size = max_buffer_size
        self.combo_window = combo_window
        self._clock = clock or time.monotonic

        if config_path is None:
            config_path = os.environ.get("INPUT_MANAGER_CONFIG", _DEFAULT_CONFIG_PATH)
        self.config_path = Path(config_path)

        self.keys: dict[str, KeyState] = {}
        # 有上限的输入缓冲，溢出时丢弃最旧事件。
        self.input_buffer: Deque[InputEvent] = deque(maxlen=self.max_buffer_size)
        self.key_bindings: dict[str, str] = {}
        self.gamepad_bindings: dict[str, str] = dict(DEFAULT_GAMEPAD_BINDINGS)
        # 连招检测用的按键历史：(按键, 时间戳)。
        self.input_history: Deque[tuple[str, float]] = deque(maxlen=16)
        # 最近一次处理的即时事件，供需要时读取。
        self.last_event: InputEvent | None = None
        # 手柄震动状态：low/high 频率强度（0.0~1.0）与持续时间。
        self.rumble: dict[str, float] = {
            "low_freq": 0.0, "high_freq": 0.0, "duration": 0.0,
        }

        self._load_config()

    # ------------------------------------------------------------------
    # 配置持久化
    # ------------------------------------------------------------------
    def _load_config(self) -> None:
        if not self.config_path.exists():
            return
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        bindings = data.get("key_bindings", {})
        if isinstance(bindings, dict):
            self.key_bindings = {
                str(action): _normalize(key)
                for action, key in bindings.items()
            }
        pad = data.get("gamepad_bindings", {})
        if isinstance(pad, dict):
            for button, action in pad.items():
                self.gamepad_bindings[_normalize(button)] = str(action)

    def _save_config(self) -> bool:
        data = {
            "key_bindings": self.key_bindings,
            "gamepad_bindings": {
                button: action
                for button, action in self.gamepad_bindings.items()
                if button not in DEFAULT_GAMEPAD_BINDINGS
                or DEFAULT_GAMEPAD_BINDINGS[button] != action
            },
        }
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.config_path)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # 1. 即时处理 + 缓冲上限
    # ------------------------------------------------------------------
    def process_input(self, key: str, pressed: bool = True,
                      timestamp: float | None = None) -> InputEvent:
        """立即处理一次按键，更新状态并放入有上限的缓冲。

        返回 InputEvent；同一帧内调用方拿到事件后可即时响应，无需等下一帧。
        """
        now = self._clock() if timestamp is None else timestamp
        norm = _normalize(key)
        action = self._action_for(norm)
        event = InputEvent(key=norm, pressed=pressed, timestamp=now, action=action)

        state = self.keys.get(norm)
        if state is None:
            state = KeyState(key=norm)
            self.keys[norm] = state

        if pressed:
            if not state.pressed:
                state.press_time = now
            state.pressed = True
            state.last_event_time = now
            self.input_history.append((norm, now))
        else:
            if state.pressed:
                state.duration = now - state.press_time
            state.pressed = False
            state.release_time = now
            state.last_event_time = now

        # deque(maxlen=...) 满时自动弹出最旧事件，连按不会无限排队。
        self.input_buffer.append(event)
        self.last_event = event
        return event

    # 语义别名：游戏循环里“立即轮询处理”。
    poll_input = process_input

    def get_buffered_inputs(self) -> list[InputEvent]:
        return list(self.input_buffer)

    def clear_buffer(self) -> None:
        self.input_buffer.clear()

    def flush(self) -> list[InputEvent]:
        """取出当前缓冲中的全部未消费事件。"""
        events = [e for e in self.input_buffer if not e.consumed]
        self.input_buffer.clear()
        return events

    def _action_for(self, norm_key: str) -> str | None:
        for action, bound in self.key_bindings.items():
            if bound == norm_key:
                return action
        return None

    # ------------------------------------------------------------------
    # 2. 按键映射：持久化 + 冲突检查
    # ------------------------------------------------------------------
    def remap_key(self, action: str, key: str) -> bool:
        """把动作绑定到按键。

        - 该按键已绑定到其他动作时拒绝（返回 False，保持原映射不变）；
        - 绑定成功后立即写入配置文件，重启后仍然生效。
        """
        action = str(action)
        norm = _normalize(key)
        if not norm:
            return False
        existing_action = self._action_for(norm)
        if existing_action is not None and existing_action != action:
            return False
        self.key_bindings[action] = norm
        return self._save_config()

    def unbind_key(self, action: str) -> bool:
        if action not in self.key_bindings:
            return False
        del self.key_bindings[action]
        return self._save_config()

    def is_key_bound(self, key: str) -> bool:
        return self._action_for(_normalize(key)) is not None

    def get_action(self, key: str) -> str | None:
        return self._action_for(_normalize(key))

    def has_conflict(self, action: str, key: str) -> bool:
        existing = self._action_for(_normalize(key))
        return existing is not None and existing != str(action)

    # ------------------------------------------------------------------
    # 3. 连招：时间窗口 + 方向键/技能键组合
    # ------------------------------------------------------------------
    def detect_combo(self,
                     sequence: Iterable,
                     timestamps: list[float] | None = None,
                     window: float | None = None) -> bool:
        """检测连招。

        sequence 为按键列表（如 ["down", "down", "J"] 或 ["A", "B"]）。
        - 相邻按键间隔必须在时间窗口内，否则不算连招；
        - 支持方向键 + 技能键的搓招组合（方向别名归一化）；
        - 未显式传入 timestamps 时使用最近记录的按键历史。
        """
        keys = [_normalize(k) for k in sequence]
        if len(keys) < 2:
            return False
        win = self.combo_window if window is None else window

        if timestamps is None:
            timestamps = self._history_timestamps_for(keys)
            if timestamps is None:
                return False
        if len(timestamps) != len(keys):
            return False

        for prev, curr in zip(timestamps, timestamps[1:]):
            delta = curr - prev
            if delta < 0 or delta > win:
                return False
        return True

    def _history_timestamps_for(self, keys: list[str]) -> list[float] | None:
        """在最近的按键历史中按顺序匹配 keys，返回对应时间戳。"""
        history = list(self.input_history)
        start = 0
        result: list[float] = []
        for target in keys:
            found = None
            for i in range(start, len(history)):
                hist_key = DIRECTION_ALIASES.get(history[i][0], history[i][0])
                wanted = DIRECTION_ALIASES.get(target, target)
                if hist_key == wanted:
                    found = i
                    break
            if found is None:
                return None
            result.append(history[found][1])
            start = found + 1
        return result

    # ------------------------------------------------------------------
    # 4. 手柄：可配置映射 + 震动
    # ------------------------------------------------------------------
    def process_gamepad(self, button: str) -> bool:
        """手柄按钮是否映射到有效动作。映射表可自定义，不再硬编码。"""
        return _normalize(button) in self.gamepad_bindings

    def get_gamepad_action(self, button: str) -> str | None:
        return self.gamepad_bindings.get(_normalize(button))

    def remap_gamepad_button(self, button: str, action: str) -> bool:
        """重映射手柄按钮，拒绝与其他按钮的动作冲突并持久化。"""
        norm = _normalize(button)
        action = str(action)
        if not norm:
            return False
        for bound_button, bound_action in self.gamepad_bindings.items():
            if bound_action == action and bound_button != norm:
                return False
        self.gamepad_bindings[norm] = action
        return self._save_config()

    def set_rumble(self,
                   low_freq: float = 0.0,
                   high_freq: float = 0.0,
                   duration: float = 0.0) -> bool:
        """触发手柄震动。强度 0.0~1.0，duration 单位秒。"""
        low_freq = min(max(float(low_freq), 0.0), 1.0)
        high_freq = min(max(float(high_freq), 0.0), 1.0)
        duration = max(float(duration), 0.0)
        self.rumble = {
            "low_freq": low_freq,
            "high_freq": high_freq,
            "duration": duration,
        }
        return low_freq > 0.0 or high_freq > 0.0

    # 语义别名。
    vibrate = set_rumble

    def stop_rumble(self) -> None:
        self.rumble = {"low_freq": 0.0, "high_freq": 0.0, "duration": 0.0}

    def is_rumbling(self) -> bool:
        return self.rumble["low_freq"] > 0.0 or self.rumble["high_freq"] > 0.0

    # ------------------------------------------------------------------
    # 5/6. 按键持续时间 + 事件消费
    # ------------------------------------------------------------------
    def get_key_state(self, key: str, now: float | None = None) -> KeyState:
        norm = _normalize(key)
        state = self.keys.get(key)
        if state is None:
            state = self.keys.get(norm)
        if state is None:
            return KeyState(key=key)
        if state.pressed:
            current = self._clock() if now is None else now
            state.duration = current - state.press_time
        return state

    def consume_event(self, event: InputEvent) -> None:
        """消费一个输入事件，其他系统看到 consumed 后应跳过。"""
        event.consume()

    def consume_last(self) -> bool:
        if self.last_event is None or self.last_event.consumed:
            return False
        self.last_event.consume()
        return True

    def consume_key(self, key: str) -> bool:
        """消费缓冲中指定按键最新的一个未消费事件。"""
        norm = _normalize(key)
        for event in reversed(self.input_buffer):
            if event.key == norm and not event.consumed:
                event.consume()
                return True
        return False

    def unconsumed_events(self) -> list[InputEvent]:
        return [e for e in self.input_buffer if not e.consumed]
