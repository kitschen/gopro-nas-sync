from gopro_sync.cameras.media import parse_media_list
from gopro_sync.cameras.models import DEFAULT, lookup
from gopro_sync.cameras.wifi import split_terse


def test_parse_single_and_grouped_media():
    data = {
        "id": "1",
        "media": [
            {
                "d": "100GOPRO",
                "fs": [
                    {"n": "GX010001.MP4", "cre": "1700000000", "mod": "1700000100", "s": "12345"},
                    {"n": "G0010010.JPG", "cre": "1700000200", "mod": "1700000200",
                     "g": "1", "s": "4", "b": "10", "l": "13", "m": ["12"], "t": "b"},
                ],
            }
        ],
    }
    media = parse_media_list(data)
    assert [m.path for m in media] == [
        "100GOPRO/GX010001.MP4", "100GOPRO/G0010010.JPG", "100GOPRO/G0010011.JPG", "100GOPRO/G0010013.JPG",
    ]
    assert media[0].size == 12345 and media[0].created == 1700000000
    assert all(m.size is None for m in media[1:])


def test_model_lookup_prefers_specific_names():
    assert lookup("HERO11 Black Mini").family == "HERO11 Mini"
    assert lookup("HERO11 Black").family == "HERO11"
    assert lookup("HERO9 Black").min_firmware == "1.60"
    assert lookup("Something new") is DEFAULT


def test_split_terse_handles_escaped_colons():
    assert split_terse(r"My\:SSID:wifi") == ["My:SSID", "wifi"]
