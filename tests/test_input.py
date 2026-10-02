"""输入管理 - 红态测试"""
import pytest, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from input.manager import InputManager, KeyState

class TestBufferLimit:
    def test_input_buffer_has_max_size(self):
        im = InputManager()
        for i in range(100):
            im.process_input(f"key{i}")
        assert len(im.input_buffer) <= 20

class TestKeyPersistence:
    def test_remap_saved_to_config(self):
        im = InputManager()
        im.remap_key("attack", "J")
        assert "attack" in im.key_bindings

class TestComboTiming:
    def test_combo_requires_timing(self):
        im = InputManager()
        # 间隔太长不算连招
        result = im.detect_combo(["A", "B"])
        assert result == False

class TestGamepadConfig:
    def test_gamepad_buttons_configurable(self):
        im = InputManager()
        result = im.process_gamepad("cross")
        assert result == True

class TestKeyDuration:
    def test_key_press_duration_tracked(self):
        im = InputManager()
        im.keys["J"] = KeyState("J", pressed=True, press_time=1.0)
        state = im.get_key_state("J")
        assert state.press_time > 0
