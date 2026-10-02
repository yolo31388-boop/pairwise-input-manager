"""输入管理 - 修复点全覆盖验证"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from input.manager import InputManager, KeyState


def make_manager(tmp_path, **kwargs):
    return InputManager(config_path=str(tmp_path / "cfg.json"), **kwargs)


class TestImmediateAndBoundedBuffer:
    def test_process_input_updates_state_immediately(self, tmp_path):
        im = make_manager(tmp_path)
        im.process_input("J")
        assert im.keys["J"].pressed is True
        assert len(im.input_buffer) == 1

    def test_buffer_drops_oldest_when_full(self, tmp_path):
        im = make_manager(tmp_path)
        for i in range(100):
            im.process_input(f"key{i}")
        assert len(im.input_buffer) == 20
        assert im.input_buffer[0].key == "key80"
        assert im.input_buffer[-1].key == "key99"


class TestRemapPersistenceAndConflict:
    def test_remap_persists_across_restart(self, tmp_path):
        cfg = str(tmp_path / "cfg.json")
        InputManager(config_path=cfg).remap_key("attack", "J")
        assert InputManager(config_path=cfg).key_bindings["attack"] == "J"

    def test_remap_conflict_rejected(self, tmp_path):
        im = make_manager(tmp_path)
        assert im.remap_key("attack", "J") is True
        assert im.remap_key("skill", "J") is False
        assert im.key_bindings["skill"] not in ("J",) if "skill" in im.key_bindings else True

    def test_remap_same_action_rebind_allowed(self, tmp_path):
        im = make_manager(tmp_path)
        im.remap_key("attack", "J")
        assert im.remap_key("attack", "K") is True
        assert im.key_bindings["attack"] == "K"


class TestComboWindowAndChords:
    def test_combo_within_window(self, tmp_path):
        im = make_manager(tmp_path)
        assert im.detect_combo([("A", 0.0), ("B", 0.3)]) is True

    def test_combo_outside_window_rejected(self, tmp_path):
        im = make_manager(tmp_path)
        assert im.detect_combo([("A", 0.0), ("B", 10.0)]) is False

    def test_direction_plus_skill_chord(self, tmp_path):
        im = make_manager(tmp_path)
        # 方向键+技能键同帧按下算一步
        assert im.detect_combo([(("down", "right"), 0.0), ("J", 0.2)]) is True
        assert im.detect_combo([{"key": ["down", "right"], "time": 0.0}, {"key": "J", "time": 0.2}]) is True

    def test_match_combo_uses_history(self, tmp_path):
        im = make_manager(tmp_path)
        im.process_input("down", timestamp=0.0)
        im.process_input("right", timestamp=0.1)
        im.process_input("J", timestamp=0.2)
        assert im.match_combo(["down", "right", "J"]) is True
        assert im.match_combo(["down", "J"]) is False


class TestGamepadConfigurableAndRumble:
    def test_ps_and_xbox_buttons_mapped(self, tmp_path):
        im = make_manager(tmp_path)
        assert im.process_gamepad("cross") is True   # PS
        assert im.process_gamepad("a") is True       # Xbox
        assert im.process_gamepad("nonexistent") is False

    def test_gamepad_remap_persists(self, tmp_path):
        cfg = str(tmp_path / "cfg.json")
        im = InputManager(config_path=cfg)
        assert im.remap_gamepad_button("cross", "taunt") is True
        assert InputManager(config_path=cfg).gamepad_bindings["cross"] == "taunt"

    def test_rumble_feedback(self, tmp_path):
        im = make_manager(tmp_path)
        assert im.rumble(0.8, 0.3) is True
        assert im.rumble_log[-1] == (0.8, 0.3)


class TestDurationAndConsumption:
    def test_long_press_distinguished(self, tmp_path):
        now = [100.0]
        im = make_manager(tmp_path, time_source=lambda: now[0])
        im.process_input("J")
        now[0] += 1.0
        assert im.is_long_press("J", threshold=0.5) is True
        assert im.get_press_duration("J") >= 1.0

    def test_event_consumed_once(self, tmp_path):
        im = make_manager(tmp_path)
        event = im.process_input("J")
        assert im.consume_event(event) is True
        assert im.consume_event(event) is False
        assert im.consume_input("J") is False  # 已被消费
