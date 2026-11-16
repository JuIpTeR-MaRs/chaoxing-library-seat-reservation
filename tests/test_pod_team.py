import base64
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zlib
import requests

import pod_reservation as pod
import pod_team as team


def profile(name="甲"):
    return dict(name=name, studentId="123456", college="现代信息产业学院", phone="13800000000")


def school_form():
    def field(ident, label, kind="editinput"):
        return {"id": ident, "compt": kind, "fields": [{"label": label,
            "options": [{"title": "现代信息产业学院"}]}]}
    result = [field(1, "申请人姓名"), field(19, "申请人学号或工号"),
              field(22, "申请人学院或部门", "selectbox"), field(14, "联系方式"), field(5, "研讨主题")]
    for ident, children in ((9, (10, 12, 25)), (27, (28, 29, 30))):
        result.append({"id": ident, "compt": "detailcombox", "label": f"研讨参与人{1 if ident == 9 else 2}信息",
            "compts": [field(children[0], "姓名"), field(children[1], "学号或工号"),
                       field(children[2], "学院或部门", "selectbox")]})
    return {"id": 590637, "version": 7, "updatetime": 1234, "formConfig": result}


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.config = {"reserve": [dict(username="one", password="p", podProfile=profile()),
                                   dict(username="two", password="p", podProfile=profile("乙"))]}
        self.plan = dict(itemId="30835", start="16:10", remark="学习", activity="多人学习", url=pod.DEFAULT_URL)

    def test_both_leader_orders_use_correct_profiles(self):
        self.assertEqual(team.validate_team_config(self.config, 0, self.plan)[1]["username"], "two")
        self.assertEqual(team.validate_team_config(self.config, 1, self.plan)[1]["username"], "one")
        self.plan["teamProfiles"] = [profile("新甲"), profile("新乙")]
        self.assertEqual(team.validate_team_config(self.config, 1, self.plan)[0]["podProfile"]["name"], "新乙")

    def test_multi_plan_uses_ninety_minutes_and_school_start(self):
        self.assertEqual(pod.validate_plan(self.plan)[:2], ("16:10", "17:40"))
        with self.assertRaises(ValueError):
            pod.validate_plan(dict(self.plan, start="16:00"))

    def test_office_cookie_handles_duplicate_domains(self):
        client = mock.Mock()
        client.session.cookies = requests.cookies.RequestsCookieJar()
        client.session.cookies.set("oa_uid", "11", domain="reserve.chaoxing.com")
        client.session.cookies.set("oa_uid", "11", domain="office.chaoxing.com")
        self.assertEqual(team.office_cookie(client, "oa_uid"), "11")

    def test_missing_or_duplicate_accounts_and_missing_fields_refused(self):
        for change in (lambda c: c["reserve"].pop(),
                       lambda c: c["reserve"][1].update(username="one"),
                       lambda c: c["reserve"][0]["podProfile"].update(studentId="")):
            config = copy.deepcopy(self.config)
            change(config)
            with self.assertRaises(ValueError):
                team.validate_team_config(config, 0, self.plan)

    def test_form_contains_one_real_member_and_no_invented_third(self):
        values = team.form_values(school_form(), profile(), profile("乙"), "多人学习")
        self.assertEqual(values["1"]["groupValues"][0]["values"], [[{"val": "甲"}]])
        self.assertEqual(values["10"]["groupValues"][0]["values"], [[{"val": "乙"}]])
        self.assertEqual(len(values["9"]["rowNos"]), 1)
        self.assertEqual(values["27"]["rowNos"], [])
        self.assertEqual(values["28"]["groupValues"], [])

    def test_form_schema_or_college_changes_stop(self):
        form = school_form()
        form["formConfig"][0]["fields"][0]["label"] = "新字段"
        with self.assertRaisesRegex(RuntimeError, "新增字段"):
            team.form_values(form, profile(), profile(), "多人学习")
        with self.assertRaisesRegex(ValueError, "当前选项"):
            team.form_values(school_form(), dict(profile(), college="不存在"), profile(), "多人学习")

    def test_form_payload_uses_official_compression_and_returned_association(self):
        client = mock.Mock()
        client.session.cookies = {"oa_uid": "123", "oa_uid_enc": "cookie"}
        client.session.post.return_value.content = json.dumps({"success": True, "data": {"postDataMap": {
            "formId": 590637, "formUserId": 77, "formData": "returned-form"}}}).encode()
        values = team.form_values(school_form(), profile(), profile("乙"), "多人学习")
        result = team.submit_form(client, ({"enc": "form-enc"}, {"forms": school_form(), "eo": "check"}), values)
        payload = client.session.post.call_args.kwargs["data"]
        decoded = zlib.decompress(bytes(x ^ 102 for x in base64.b64decode(payload["formIdValueData"])))
        self.assertEqual(json.loads(decoded), values)
        self.assertEqual(payload["checkCode"], "check")
        self.assertEqual(result["formUserId"], 77)

    def test_team_occupancy_counts_a_team_as_one_booking(self):
        client = pod.PodClient.__new__(pod.PodClient)
        client.reserve_id, client.enc, client.fid = "15133", "enc", "fid"
        day = str(dt.datetime.now(pod.TZ).date() + dt.timedelta(days=1))
        start, end = pod.date_bounds(day, "16:10", "17:40")
        client.request = mock.Mock(side_effect=[
            {"reserveItemData": {"isSpecial": 0, "isForms": 1}, "reserveData": {"multiReserve": 2, "timeModel": 1}},
            {"commonIntervalMap": {str(start.isoweekday()): [{"id": 1, "startTime": "16:10", "endTime": "17:40", "available": 1, "userNum": 1}]}},
            {"reserveUserList": [{"starttime": start.timestamp()*1000, "endtime": end.timestamp()*1000, "reserveNum": 2}]}])
        self.assertFalse(client.slots("30835", day)[0]["free"])

    def test_single_pod_keeps_existing_booking_path(self):
        client = mock.Mock()
        plan = dict(self.plan, itemId="38541")
        team.book_with_accounts(client, {}, 0, plan, "2026-11-16", "config.local.json")
        client.book.assert_called_once_with(plan, "2026-11-16")

    def test_partner_duplicate_does_not_create_team(self):
        client, partner = mock.Mock(), mock.Mock()
        partner.matching.return_value = [{"id": 1}]
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(team, "PodClient", return_value=partner):
            result = team.book_with_accounts(client, self.config, 0, self.plan, "2026-11-16", Path(folder)/"config.json")
        self.assertIn("未重复提交", result)
        client.book.assert_not_called()

    def test_submit_timeout_verifies_success_and_journal_blocks_retry(self):
        client, partner = mock.Mock(), mock.Mock()
        partner.matching.return_value = []
        client.reserve_id, client.enc, client.fid = "15133", "enc", "fid"
        client.session.cookies = {"oa_uid": "11"}
        partner.session.cookies = {"oa_uid": "22"}
        client.params.side_effect = lambda **kw: kw
        client.request.side_effect = [{"reserveNum": 1}, TimeoutError("timeout")]
        client.matching.return_value = [{"id": 1}]
        slot = {"id": 42, "start": "16:10", "end": "17:40"}
        client.book.side_effect = lambda plan, day, team_booking: team_booking(client, plan, day, slot)
        responses = [{"leader": True, "reserveTeam": {"id": 99, "minUserNum": 2, "maxUserNum": 4}},
                     {}, {"leader": True, "joinNum": 2}, {"teamUsers": [{"uid": 11}, {"uid": 22}]}, {}]
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(team, "PodClient", return_value=partner), \
                mock.patch.object(team, "form_context", return_value=({}, {"forms": school_form()})), \
                mock.patch.object(team, "team_request", side_effect=responses) as requests, \
                mock.patch.object(team, "submit_form", return_value={"formData": "form", "formUserId": 77}):
            path = Path(folder)/"config.json"
            result = team.book_with_accounts(client, self.config, 0, self.plan, "2026-11-16", path)
            self.assertIn("预约成功", result)
            self.assertEqual(client.request.call_count, 2)
            self.assertEqual(requests.call_count, 5)
            submit = client.request.call_args.kwargs
            self.assertTrue(submit["post"])
            payload = client.request.call_args.args[1]
            self.assertEqual(payload["teamId"], 99)
            self.assertNotIn("reserveUsers", payload)
            with self.assertRaisesRegex(RuntimeError, "已尝试过"):
                team.book_with_accounts(client, self.config, 0, self.plan, "2026-11-16", path)
            self.assertEqual(requests.call_count, 5)

    def test_ambiguous_team_creation_is_never_repeated(self):
        client, partner = mock.Mock(), mock.Mock()
        partner.matching.return_value = []
        slot = {"id": 42, "start": "16:10", "end": "17:40"}
        client.book.side_effect = lambda plan, day, team_booking: team_booking(client, plan, day, slot)
        client.fid, client.reserve_id = "fid", "15133"
        client.request.return_value = {"reserveNum": 1}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(team, "PodClient", return_value=partner), \
                mock.patch.object(team, "form_context", return_value=({}, {"forms": school_form()})), \
                mock.patch.object(team, "team_request", side_effect=TimeoutError()) as requests:
            path = Path(folder)/"config.json"
            with self.assertRaises(TimeoutError):
                team.book_with_accounts(client, self.config, 0, self.plan, "2026-11-16", path)
            with self.assertRaisesRegex(RuntimeError, "已尝试过"):
                team.book_with_accounts(client, self.config, 0, self.plan, "2026-11-16", path)
            self.assertEqual(requests.call_count, 1)


if __name__ == "__main__":
    unittest.main()
