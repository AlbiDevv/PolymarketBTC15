import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.telegram_acl import TelegramACL


def test_single_user_mode():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        acl = TelegramACL(root, primary_chat_id="111", admin_chat_id="")
        assert acl.is_allowed("111")
        assert not acl.is_allowed("222")


def test_multi_user_admin():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        acl = TelegramACL(root, primary_chat_id="111", admin_chat_id="999")
        assert acl.is_admin("999")
        assert acl.is_allowed("999")
        assert not acl.is_allowed("111")
        acl.approve("222")
        assert not acl.is_allowed("222")
        assert acl.list_allowed() == []


def test_multi_user_multiple_admins():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        acl = TelegramACL(root, primary_chat_id="111", admin_chat_id=["999", "888"])
        assert acl.is_admin("999")
        assert acl.is_admin("888")
        assert acl.is_allowed("999")
        assert acl.is_allowed("888")
        assert sorted(acl.admin_ids) == ["888", "999"]


def test_multi_user_mode_ignores_pending_and_whitelist():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        acl = TelegramACL(root, primary_chat_id="111", admin_chat_id=["999", "888"])
        acl.add_pending("222", "user222")
        acl.add_user_manual("333")
        assert not acl.is_pending("222")
        assert not acl.is_allowed("333")
        assert acl.list_allowed() == []
