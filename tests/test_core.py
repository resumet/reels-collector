import unittest

from app.providers.base import find_media_dicts, parse_ig_media

SAMPLE = {
    "stream_rows": [{"data": {"items": [
        {"__typename": "XDTUserDict", "pk": "1", "username": "someone"},
        {"__typename": "XDTMediaDict", "pk": "111", "id": "111_9", "code": "AbC", "media_type": 2,
         "product_type": "clips", "taken_at": 1764000000, "video_duration": 12.5, "play_count": 1000,
         "like_count": 50, "comment_count": 3, "reshare_count": 7,
         "user": {"pk": "9", "username": "u9", "full_name": "U", "is_verified": True},
         "caption": {"text": "hello #카페 #vlog"},
         "video_versions": [{"url": "low", "width": 360, "height": 640}, {"url": "high", "width": 720, "height": 1280}],
         "image_versions2": {"candidates": [{"url": "t", "width": 720, "height": 1280}]},
         "clips_metadata": {"music_info": {"music_asset_info": {"title": "Song", "display_artist": "Artist"}}}},
        {"__typename": "XDTMediaDict", "pk": "222", "code": "Pho", "media_type": 1, "product_type": "feed"},
        {"__typename": "XDTMediaDict", "pk": "333", "code": "Car", "media_type": 8, "product_type": "carousel_container",
         "carousel_media": [{"pk": "334", "code": "x", "media_type": 2}]},
    ]}}],
    "page_info": {"end_cursor": "abc", "has_next_page": True},
}


class ParserTest(unittest.TestCase):
    def test_finds_media_and_filters_reels(self):
        medias = find_media_dicts(SAMPLE)
        self.assertEqual([m["code"] for m in medias], ["AbC", "Pho", "Car"])  # 캐러셀 내부로 내려가지 않음
        items = [parse_ig_media(m, "reels_serp") for m in medias]
        self.assertEqual([i.is_reel for i in items], [True, False, False])
        r = items[0]
        self.assertEqual((r.media_pk, r.video_url, r.username, r.music_title), ("111", "high", "u9", "Song"))
        self.assertEqual(r.hashtags, ["카페", "vlog"])
        self.assertEqual(r.share_count, 7)


if __name__ == "__main__":
    unittest.main()
