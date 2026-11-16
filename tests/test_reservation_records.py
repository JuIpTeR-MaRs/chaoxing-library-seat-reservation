import unittest
from unittest.mock import Mock

from reservation_records import cancel_reservation, list_reservations, reservation_location


class ReservationRecordsTest(unittest.TestCase):
    def test_location_keeps_full_hierarchy_and_seat_leading_zeros(self):
        self.assertEqual(reservation_location({
            "firstLevelName": "图书馆", "secondLevelName": "三楼",
            "thirdLevelName": "经典文库阅览学习区", "seatNum": "002"}),
            ("图书馆 / 三楼 / 经典文库阅览学习区", "002"))

    def test_location_omits_empty_levels(self):
        self.assertEqual(reservation_location({
            "firstLevelName": " 图书馆 ", "secondLevelName": None,
            "thirdLevelName": "阅览区", "seatNum": 0}), ("图书馆 / 阅览区", "0"))

    def test_location_falls_back_when_names_are_missing(self):
        self.assertEqual(reservation_location({"roomName": "北区", "seatNum": "010"}),
                         ("北区", "010"))
        self.assertEqual(reservation_location({"roomId": 11031}), ("区域 11031", "未知座位"))
        self.assertEqual(reservation_location({}), ("未知区域", "未知座位"))

    def test_list_uses_account_session_and_reads_records(self):
        client = Mock()
        client.requests.get.return_value.json.return_value = {
            "success": True, "data": {"curReserves": [{"id": 12, "status": 0}]}}
        self.assertEqual(list_reservations(client, "abc123"), [{"id": 12, "status": 0}])
        client.requests.get.assert_called_once_with(
            "https://office.chaoxing.com/data/apps/seat/index",
            params={"fidEnc": "abc123"})

    def test_current_records_are_read_even_when_history_is_empty(self):
        client = Mock()
        client.requests.get.return_value.json.return_value = {
            "success": True, "data": {"reserveList": [], "curReserves": [
                {"id": 12, "status": 1}, {"id": 13, "status": 0}]}}
        self.assertEqual(len(list_reservations(client, "abc123")), 2)
        self.assertEqual(client.requests.get.call_count, 1)

    def test_cancel_uses_selected_record_id_and_checks_business_result(self):
        client = Mock()
        client.requests.get.return_value.json.return_value = {"success": False, "msg": "不可取消"}
        with self.assertRaisesRegex(RuntimeError, "不可取消"):
            cancel_reservation(client, 12)
        client.requests.get.assert_called_once_with(
            "https://office.chaoxing.com/data/apps/seat/cancel", params={"id": "12"})

    def test_invalid_id_does_not_call_server(self):
        client = Mock()
        with self.assertRaises(ValueError):
            cancel_reservation(client, "12&other=3")
        client.requests.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
