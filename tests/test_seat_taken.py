"""Offline checks for the seat-taken and already-held rejection classification."""

import unittest

import reserve_segments


class SeatTakenMessageTests(unittest.TestCase):
    def test_recognizes_seat_taken_wordings(self):
        for text in ("该时段已被预约", "该时段已经预约", "该座位已预约", "预约时间冲突",
                     "该时间段已被占用！", "该时段已被占用"):
            self.assertTrue(reserve_segments.seat_taken_message(text))

    def test_ignores_other_rejections(self):
        for text in ("当前区域未到开放预约时间，请咨询管理员确认该区域开放时间",
                     "验证码校验失败", "", None):
            self.assertFalse(reserve_segments.seat_taken_message(text))

    def test_seat_taken_does_not_claim_already_held(self):
        self.assertFalse(reserve_segments.seat_taken_message("当前时间段您已有预约，请选择其他时间段"))


class SeatHeldMessageTests(unittest.TestCase):
    def test_recognizes_already_held_wording(self):
        self.assertTrue(reserve_segments.seat_held_message(
            "当前时间段您已有预约，请选择其他时间段"))

    def test_ignores_seat_taken_wording(self):
        self.assertFalse(reserve_segments.seat_held_message("该时间段已被占用！"))
        self.assertFalse(reserve_segments.seat_held_message(""))


if __name__ == "__main__":
    unittest.main()
