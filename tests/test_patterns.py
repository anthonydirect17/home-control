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


def test_pulse_wave_strobe():
    from home_control.patterns import render
    red = {"color": "#FF0000", "color2": "#000000"}
    peak = render("pulse", 0.1, 3, dict(red, rate=1))[0]          # at 10% of a pulse: rising
    late = render("pulse", 0.9, 3, dict(red, rate=1))[0]          # near the end: faded
    assert peak[0] > late[0] and peak[1] == peak[2] == 0
    dim = render("pulse", 0.15, 1, dict(red, rate=1, brightness=0.5))[0]
    assert 120 <= dim[0] <= 130
    w = render("wave", 0, 8, {"color": "#FFFFFF", "color2": "#000000", "speed": 1})
    assert len({px for px in w}) > 2                               # a gradient along the strip
    assert render("wave", 1, 8, {"speed": 1}) == render("wave", 0, 8, {"speed": 1})  # one full wave later
    on = render("strobe", 0.1, 2, {"rate": 2, "on": 0.5})[0]
    off = render("strobe", 0.3, 2, {"rate": 2, "on": 0.5})[0]
    assert on == (255, 255, 255) and off == (0, 0, 0)
