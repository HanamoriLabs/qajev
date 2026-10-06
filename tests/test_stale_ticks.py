"""Digits ticking in what a person reads do not make Jev's decision stale; any other change does (verse2, 6 Oct)."""

from qajev.session import fresh_past_ticks


class FakeBrowser:
    def __init__(self, answer):
        self.answer = answer

    def evaluate(self, _expression):
        return self.answer


def marker(text, title="Town square", href="http://127.0.0.1/t", origin=1.0, label="Enter the hall", inputs=None):
    return [origin, href, 0, 0, 1280, 800, title, text, [{"node": 3, "label": label, "kind": "click"}], inputs or []]


def guard(card, href="/hall", name="Enter the hall", value=None):
    return [3, "button", name, value, None, None, None, False, None, None, None, None, href, card]


PAGE = {"marker": marker("Big screen 0:03 / 188:26\nPress E to enter."), "page_key": [1.0, "http://127.0.0.1/t", 0, 0],
        "guards": {"3": guard("Enter the hall Doors close in 59 s")}}
CLICK = {"kind": "click", "node": 3}


def test_a_ticking_clock_or_countdown_is_not_a_change():
    assert fresh_past_ticks(FakeBrowser(marker("Big screen 0:07 / 188:26\nPress E to enter.")), "", PAGE)
    assert fresh_past_ticks(FakeBrowser(marker("Big screen 0:07 / 188:26\nPress E to enter.", label="Enter the hall")),
                            "", PAGE)
    now = [[1.0, "http://127.0.0.1/t", 0, 0], guard("Enter the hall Doors close in 55 s")]
    assert fresh_past_ticks(FakeBrowser(now), "", PAGE, CLICK)
    assert fresh_past_ticks(FakeBrowser(marker("x", label="Resend in 0:29")), "",
                            {**PAGE, "marker": marker("x", label="Resend in 0:30")})


def test_new_words_a_reload_the_address_an_input_or_a_link_still_are():
    assert not fresh_past_ticks(FakeBrowser(marker("Big screen 0:07 / 188:26\nThe hall is closed.")), "", PAGE)
    reloaded = marker("Big screen 0:03 / 188:26\nPress E to enter.", origin=2.0)
    assert not fresh_past_ticks(FakeBrowser(reloaded), "", PAGE)
    assert not fresh_past_ticks(FakeBrowser(marker("Big screen 0:03 / 188:26\nPress E to enter.",
                                                   href="http://127.0.0.1/u")), "", PAGE)
    assert not fresh_past_ticks(FakeBrowser(marker("Big screen 0:03 / 188:26\nPress E to enter.",
                                                   inputs=[[5, "12", False, -1, False, False]])), "", PAGE)
    key = [1.0, "http://127.0.0.1/t", 0, 0]
    assert not fresh_past_ticks(FakeBrowser([key, guard("Enter the hall Doors close in 55 s", href="/hall/2")]),
                                "", PAGE, CLICK)  # a link's address is compared exactly, digits included
    assert not fresh_past_ticks(FakeBrowser([key, guard("The hall is closed")]), "", PAGE, CLICK)
    scrolled = [[1.0, "http://127.0.0.1/t", 0, 560], guard("Enter the hall Doors close in 55 s")]
    assert not fresh_past_ticks(FakeBrowser(scrolled), "", PAGE, CLICK)
    assert not fresh_past_ticks(FakeBrowser(None), "", PAGE, CLICK)


def test_counts_steps_prices_and_labels_are_read_exactly():
    # 0.4.0 review (SideGame3): only time-like digits tick. A step, a count, a price or a button's own number that
    # changes is the page moving on, so the decision is made again.
    def fresh(before, after, **kw):
        return fresh_past_ticks(FakeBrowser(marker(after, **kw)), "", {**PAGE, "marker": marker(before, **kw)})

    assert not fresh("Step 1 of 3", "Step 2 of 3")
    assert not fresh_past_ticks(FakeBrowser(marker("x", label="Cart (2)")), "",
                                {**PAGE, "marker": marker("x", label="Cart (1)")})
    assert not fresh("Total $5", "Total $500")
    buy = {**PAGE, "guards": {"3": guard("Buy 1", name="Buy 1")}}
    assert not fresh_past_ticks(FakeBrowser([[1.0, "http://127.0.0.1/t", 0, 0], guard("Buy 100", name="Buy 100")]),
                                "", buy, CLICK)
    assert fresh("Round ends in 1:05:09", "Round ends in 1:04:58")  # hours too
    assert fresh("Retry in 30 sec", "Retry in 9 sec") and fresh("Closes in 5 min", "Closes in 4 min")
