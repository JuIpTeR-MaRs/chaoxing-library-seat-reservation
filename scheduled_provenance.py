"""Conservative evidence for records created by a scheduled booking run."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

STATE = "scheduled-provenance.json"


def identity(username):
    return hashlib.sha256(str(username).encode("utf-8")).hexdigest()


def fingerprint(record):
    return {key: str(record[key]) for key in
            ("id", "roomId", "seatNum", "startTime", "endTime")}


def read(config_path):
    try:
        data = json.loads(Path(config_path).with_name(STATE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("version") == 1 else {}
    except (OSError, ValueError):
        return {}


def save(config_path, username, day, before, attempts, after):
    # The caller holds the shared evidence lock. Never infer ownership from a
    # final coverage check alone, including an already-held submit response.
    data = read(config_path)
    accounts = data.setdefault("accounts", {})
    key = identity(username)
    evidence = []
    if before is not None and after is not None:
        for record in after:
            try:
                item = fingerprint(record)
                if int(record.get("status", -1)) != 0:
                    continue
                begin, end = int(item["startTime"]), int(item["endTime"])
                spans = []
                for attempt in attempts:
                    if str(attempt.get("day")) != str(day):
                        continue
                    if (str(attempt.get("roomId")), str(attempt.get("seatNum"))) != (item["roomId"], item["seatNum"]):
                        continue
                    tz = dt.timezone(dt.timedelta(hours=8))
                    spans.append(tuple(int(dt.datetime.fromisoformat(f"{day}T{attempt[k]}").replace(tzinfo=tz).timestamp() * 1000)
                                       for k in ("startTime", "endTime")))
                # Reject any existing record overlapping this record, even when
                # the school changed its ID while merging adjacent submissions.
                if any(str(old.get("id")) == item["id"] or
                       int(old["startTime"]) < end and begin < int(old["endTime"])
                       for old in before):
                    continue
                cursor = begin
                for start, finish in sorted(spans):
                    if start < begin or finish > end:
                        continue
                    if start <= cursor:
                        cursor = max(cursor, finish)
                if begin < end and cursor >= end:
                    evidence.append(item)
            except (KeyError, TypeError, ValueError):
                continue
    accounts[key] = {"date": str(day), "records": evidence,
                     "before": before, "attempts": attempts}
    data["version"] = 1
    path = Path(config_path).with_name(STATE)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    os.replace(temp, path)


def eligible(data, username, day, record):
    try:
        account = data["accounts"][identity(username)]
        return account["date"] == str(day) and fingerprint(record) in account["records"]
    except (KeyError, TypeError, ValueError):
        return False
