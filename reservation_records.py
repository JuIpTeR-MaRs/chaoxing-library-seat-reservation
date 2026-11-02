"""Read and cancel reservations for the signed-in ChaoXing account."""

BASE = "https://office.chaoxing.com/data/apps/seat"


def _response_json(response):
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise RuntimeError((payload.get("msg") if isinstance(payload, dict) else None)
                           or "学习通未确认操作成功。")
    return payload


def list_reservations(client, fid_enc):
    records = []
    seen = set()
    payload = _response_json(client.requests.get(
        BASE + "/index", params={"fidEnc": fid_enc},
    ))
    data = payload.get("data") or {}
    batch = data.get("curReserves")
    if not isinstance(batch, list):
        raise RuntimeError("当前预约返回格式不正确。")
    for item in batch:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        record_id = str(item["id"])
        if record_id not in seen:
            records.append(item)
            seen.add(record_id)
    return records


def cancel_reservation(client, record_id):
    if not str(record_id).isdecimal():
        raise ValueError("预约记录 ID 无效。")
    return _response_json(client.requests.get(
        BASE + "/cancel", params={"id": str(record_id)}))
