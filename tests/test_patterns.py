import pytest

from home_control import patterns


@pytest.mark.parametrize("name", list(patterns.PATTERNS))
@pytest.mark.parametrize("n", [1, 5, 20])
def test_every_pattern_returns_one_valid_color_per_segment(name, n):
    defaults = {p["key"]: p["default"] for p in patterns.PATTERNS[name][2]}
    for t in (0, 0.37, 5.2, 123.4):
        colors = patterns.render(name, t, n, defaults)
        assert len(colors) == n
        for c in colors:
            assert len(c) == 3 and all(0 <= x <= 255 and isinstance(x, int) for x in c)


def test_chase_moves_over_time():
    p = {"colors": "#FF0000,#FFFFFF", "width": 1, "speed": 1}
    assert patterns.render("chase", 0, 4, p) != patterns.render("chase", 1, 4, p)
    assert patterns.render("chase", 0, 4, p) == patterns.render("chase", 2, 4, p)


def test_twinkle_is_repeatable_for_the_same_time():
    p = {"density": 0.5}
    assert patterns.render("twinkle", 3.3, 30, p) == patterns.render("twinkle", 3.3, 30, p)


def test_hex_round_trip_and_short_form():
    assert patterns.rgb_to_hex(patterns.hex_to_rgb("#ffb46b")) == "#FFB46B"
    assert patterns.hex_to_rgb("f00") == (255, 0, 0)
    with pytest.raises(ValueError):
        patterns.hex_to_rgb("#12")


def test_unknown_pattern():
    with pytest.raises(KeyError):
        patterns.render("nope", 0, 3, {})
