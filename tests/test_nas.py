import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

from gopro_sync import nas

HELPER_PATH = Path(__file__).parents[1] / "packaging" / "gopro-sync-mount"


def load_helper():
    loader = importlib.machinery.SourceFileLoader("gopro_sync_mount", str(HELPER_PATH))
    spec = importlib.util.spec_from_loader("gopro_sync_mount", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


helper = load_helper()


def test_smb_unit_and_credentials():
    settings = helper.validate({"type": "smb", "server": "nas.local", "share": "media/gopro", "version": "3.0",
                                "username": "phil", "domain": "", "password": "s3cret=!"})
    unit = helper.render_unit(settings, 999, 998)
    assert "What=//nas.local/media/gopro" in unit
    assert "Type=cifs" in unit
    assert "uid=999,gid=998" in unit and "vers=3.0" in unit and "_netdev" in unit
    assert "s3cret" not in unit
    assert helper.render_credentials(settings, "s3cret=!") == "username=phil\npassword=s3cret=!\n"


def test_nfs_unit():
    settings = helper.validate({"type": "nfs", "server": "192.168.1.10", "share": "/volume1/media", "version": "4.1"})
    unit = helper.render_unit(settings, 0, 0)
    assert "What=192.168.1.10:/volume1/media" in unit and "nfsvers=4.1" in unit


@pytest.mark.parametrize("bad", [
    {"type": "smb", "server": "nas;rm -rf /", "share": "x"},
    {"type": "smb", "server": "nas", "share": "a,credentials=/etc/shadow"},
    {"type": "smb", "server": "nas", "share": "ok", "password": "line\nbreak"},
    {"type": "nfs", "server": "nas", "share": "relative/path"},
    {"type": "ext4", "server": "nas", "share": "/x"},
])
def test_helper_rejects_bad_input(bad):
    with pytest.raises(helper.ValidationError):
        helper.validate(bad)


def test_parse_route_get():
    out = '[{"dst":"192.168.1.10","gateway":"192.168.1.1","dev":"eth0","prefsrc":"192.168.1.5"}]'
    assert nas.parse_route_get(out) == "eth0"
    assert nas.parse_route_get("garbage") is None


def test_nas_in_camera_network_is_rejected():
    info = nas.check_route("10.5.5.20", "eth0")
    assert not info.ok and "camera network" in info.message
