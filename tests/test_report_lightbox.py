"""Every screenshot in report.html opens in a lightbox (José, 12 Oct, on a FIGURE OK run: "This needs to open in
lightbox"). The page must work where no script runs: QAJev Cloud serves report.html with a sandbox CSP that allows no
script at all. So a thumbnail is a link to its overlay (#shot-N, shown by CSS :target), the overlay's links move
between screenshots and close it, and a small inline script adds Esc, the arrow keys and focus where scripts run.
"""

import json
import re
from pathlib import Path

from qajev import report_html

SIX_GAME = Path(__file__).parent / "fixtures" / "six-game-report.json"


def page_with_shots():
    data = json.loads(SIX_GAME.read_text())
    data["scenarios"][0]["shot"] = "shots/arm-wrestle.jpg"
    data["scenarios"][2]["shot"] = "shots/claw.jpg"
    data["scenarios"][4]["react"] = [{"path": "shots/karaoke-cue.jpg", "shot": "the spark card", "at_s": 3.5}]
    return report_html.render(data)


def overlay(page, n):
    start = page.index(f'id="shot-{n}"')
    return page[start:page.index("</div>", start)]


def test_a_thumbnail_opens_its_screenshot_full_size_with_the_test_and_step_as_caption():
    page = page_with_shots()
    thumbs = re.findall(r'<a class="shot" id="shot-(\d+)-at" href="#shot-(\d+)" title="([^"]+)"', page)
    assert [(a, b) for a, b, _ in thumbs] == [("1", "1"), ("2", "2"), ("3", "3")]  # in page order
    assert thumbs[0][2] == "Open the screenshot: arm wrestle: follow the card · the last screen"
    first = overlay(page, 1)
    assert '<img src="shots/arm-wrestle.jpg"' in first
    assert "arm wrestle: follow the card · the last screen · 1 of 3" in first
    assert "karaoke: the lane and the spark card · the spark card at 3.5 s" in overlay(page, 3)  # a react frame
    assert ".lb:target" in page  # shown without a script, as the sandboxed cloud copy needs


def test_the_overlay_moves_between_screenshots_and_closes_back_to_its_thumbnail():
    page = page_with_shots()
    first, middle, last = overlay(page, 1), overlay(page, 2), overlay(page, 3)
    assert 'href="#shot-2"' in first and "lb-prev" not in first
    assert 'href="#shot-1"' in middle and 'href="#shot-3"' in middle
    assert 'href="#shot-2"' in last and "lb-next" not in last
    assert first.count('href="#shot-1-at"') == 2  # the Close link and a click outside the image


def test_esc_closes_it_and_the_arrows_move_where_scripts_run():
    page = page_with_shots()
    script = page[page.index("<script>"):page.index("</script>")]
    assert "'Escape'" in script and "'ArrowRight'" in script and "'ArrowLeft'" in script
    assert "-at" in script and ".focus()" in script  # focus goes back to the thumbnail
    assert "src=" not in script.split("<script>", 1)[1]  # an inline script only: the file works offline


def test_a_report_without_screenshots_has_no_overlay_or_script():
    page = report_html.render(json.loads(SIX_GAME.read_text()))
    assert 'class="lb"' not in page and "<script>" not in page
