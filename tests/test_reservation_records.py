import unittest
from unittest.mock import Mock

from reservation_records import cancel_reservation, list_reservations


class ReservationRecordsTest(unittest.TestCase):
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
