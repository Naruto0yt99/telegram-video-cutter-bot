from PIL import Image

from visual_matcher import _distance, _normalize_region, _signature, _unique_starts


def test_fraction_and_percent_regions():
    assert _normalize_region((0.1, 0.2, 0.3, 0.4)) == (0.1, 0.2, 0.3, 0.4)
    assert _normalize_region((10, 20, 30, 40)) == (0.1, 0.2, 0.3, 0.4)


def test_bad_region_is_rejected():
    assert _normalize_region((1, 2, 3)) is None


def test_identical_image_signatures_match():
    signature = _signature(Image.new("RGB", (64, 48), (120, 80, 200)))
    assert _distance(signature, signature) < 1e-6


def test_unique_starts_deduplicates_and_clamps():
    assert _unique_starts([-2, 1.04, 1.03, 99], 10) == [0.0, 1.0, 10.0]
