from __future__ import annotations

import pytest

from desktop.ui.models import ActionReceipt, PayloadError


def test_command_results_survive_the_desktop_boundary():
    payload = {"action": "research-review_save", "accepted": True,
               "message": "已保存", "request_id": "test-1",
               "result": {"report_id": "report-1", "revision": 2}}
    receipt = ActionReceipt.from_payload(payload)
    assert receipt.result == payload["result"]
    assert ActionReceipt.from_payload({key: value for key, value in payload.items()
                                      if key != "result"}).result is None
    with pytest.raises(PayloadError):
        ActionReceipt.from_payload({**payload, "result": "not-an-object"})
