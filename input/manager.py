"""输入管理 - 含5个bug"""
from dataclasses import dataclass, field

@dataclass
class KeyState:
    key: str
    pressed: bool = False
    press_time: float = 0.0  # bug5: 不记录持续时间

class InputManager:
    def __init__(self):
        self.keys: dict[str, KeyState] = {}
        self.input_buffer: list = []  # bug1: 无上限
        self.key_bindings: dict[str, str] = {}  # bug2: 不持久化
        self.combo_window: float = 0.5  # bug3: 无时间窗口

    def process_input(self, key: str) -> None:
        # bug1: 输入缓冲无上限
        self.input_buffer.append(key)

    def remap_key(self, action: str, key: str) -> bool:
        # bug2: 不保存到配置
        self.key_bindings[action] = key
        return True

    def detect_combo(self, sequence: list) -> bool:
        # bug3: 不检查时间间隔
        return len(sequence) >= 2

    def process_gamepad(self, button: str) -> bool:
        # bug4: 硬编码按键
        return button == "A"

    def get_key_state(self, key: str) -> KeyState:
        # bug5: 不记录持续时间
        return self.keys.get(key, KeyState(key))
