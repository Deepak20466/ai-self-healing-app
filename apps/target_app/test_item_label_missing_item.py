"""Regression test for heal_job #330 (sentinel error #131).

`bugs.item_label()` dereferenced `item.name` without checking whether the
lookup returned anything, so requesting a label for a nonexistent item
raised `AttributeError` instead of a clean 404. This file lives outside
`tests/`, so none of `tests/conftest.py`'s DB-isolation fixtures apply here
— the missing lookup is exercised by monkeypatching `repository.get_item`
directly, so the test never touches a database.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from apps.target_app import bugs


async def test_item_label_missing_item_raises_http_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_item(session: object, item_id: int) -> None:
        return None

    monkeypatch.setattr(bugs.repository, "get_item", fake_get_item)

    with pytest.raises(HTTPException) as exc_info:
        await bugs.item_label(None, 999)

    assert exc_info.value.status_code == 404
