"""Stopping the app never leaves a siren on: rules and sirens stop before the Ring helper."""

from home_control.__main__ import shutdown


class Part:
    def __init__(self, name, log, fail=False):
        self.name, self.log, self.fail = name, log, fail

    def __getattr__(self, method):
        def call():
            self.log.append(f"{self.name}.{method}")
            if self.fail:
                raise RuntimeError("stuck")
        return call


def test_shutdown_order_and_resilience():
    log = []
    p = {n: Part(n, log) for n in ("scheduler", "poller", "rules", "sirens", "ring", "sonos", "controller", "hub")}
    p["poller"].fail = True                      # one part failing doesn't stop the rest
    shutdown(p["scheduler"], p["poller"], p["rules"], p["sirens"], p["ring"], {"sonos": p["sonos"]},
             p["controller"], p["hub"])
    assert log == ["scheduler.stop", "poller.stop", "rules.stop", "sirens.stop_all", "ring.stop",
                   "sonos.stop", "controller.stop_all", "hub.close"]


def test_shutdown_without_ring():
    log = []
    p = {n: Part(n, log) for n in ("scheduler", "poller", "rules", "controller", "hub")}
    shutdown(p["scheduler"], p["poller"], p["rules"], None, None, {}, p["controller"], p["hub"])
    assert log == ["scheduler.stop", "poller.stop", "rules.stop", "controller.stop_all", "hub.close"]
