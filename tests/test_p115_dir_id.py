"""115 目录 id 解析：不同接口版本返回的键名不一致（id / file_id / cid）。"""

import pytest

from relay.p115 import P115Error, _dir_id


@pytest.mark.parametrize("response,expected", [
    ({"state": True, "data": {"id": 123}}, 123),
    ({"state": True, "data": {"file_id": "456"}}, 456),
    ({"state": True, "data": {"cid": 789}}, 789),
    ({"state": True, "id": 321}, 321),
    ({"state": True, "file_id": "654"}, 654),
])
def test_dir_id_accepts_alternate_keys(response, expected):
    assert _dir_id(response) == expected


def test_dir_id_raises_without_id():
    with pytest.raises(P115Error):
        _dir_id({"state": True, "data": {"is_private": "0"}})
