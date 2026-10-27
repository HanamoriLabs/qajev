"""A planted control's gate grade must be clear in the human HTML report."""

import copy
from html.parser import HTMLParser

import pytest

from qajev import report_html
from test_expect_fail import run


class Report(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.pills = []
        self.pill_contexts = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.stack.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, text):
        self.text.append(text)
        if self.stack and 'pill' in self.stack[-1][1].get('class', '').split():
            self.pills.append((self.stack[-1][1]['class'].split(), text))
            self.pill_contexts.append({'text': text, 'classes': self.stack[-1][1]['class'].split(),
                                      'section': next((attrs.get('id') for tag, attrs in self.stack
                                                       if tag == 'section'), None),
                                      'table': any(tag == 'table' for tag, _ in self.stack)})


def test_caught_plant_is_green_in_scenario_table_and_card(tmp_path):
    data = run(tmp_path, js="the price is 12")
    before = copy.deepcopy(data)
    page = Report(report_html.render(data))
    caught = [(row['classes'], row['text']) for row in page.pill_contexts if row['text'] == "CAUGHT"
              and (row['table'] or row['section'] == 's1')]
    assert len(caught) == 2
    assert all('pass' in classes and 'fail' not in classes for classes, _ in caught)
    assert '1 plant caught' in ''.join(page.text)
    assert not any('fail' in classes for classes, _ in page.pills)
    assert data == before, "presentation must preserve raw checks, counts and grades"


def test_caught_plant_has_its_own_count_and_no_product_failure_explanation(tmp_path):
    data = run(tmp_path, js="the price is 12")
    page = Report(report_html.render(data))
    text = ''.join(page.text)
    assert '1 pass' in text and '1 plant caught' in text
    assert not any(label == '1 fail' for _, label in page.pills)
    assert 'the product is wrong' not in text


@pytest.mark.parametrize('changes', [{}, {'shows': False}, {'shows': False, 'js': 'the price is 12'}])
def test_missed_plant_stays_red_and_says_the_check_missed_the_fault(tmp_path, changes):
    data = run(tmp_path, **changes)
    page = Report(report_html.render(data))
    text = ''.join(page.text)
    assert 'the check missed a planted fault' in text
    assert '1 plant missed' in text
    assert any('fail' in classes and label == 'FAIL' for classes, label in page.pills)
    assert not any(label == 'CAUGHT' for _, label in page.pills)
    assert 'the product is wrong' not in text


@pytest.mark.parametrize('outcome', ['harness', 'stuck', 'skipped'])
def test_interrupted_plant_never_becomes_a_catch_or_a_missed_check(tmp_path, outcome):
    page = Report(report_html.render(run(tmp_path, js='the price is 12', outcome=outcome)))
    assert not any(label == 'CAUGHT' for _, label in page.pills)
    assert 'the check missed a planted fault' not in ''.join(page.text)


def test_real_product_failure_remains_red_beside_a_caught_plant(tmp_path):
    data = run(tmp_path, js='the price is 12')
    data['scenarios'][0].update(outcome='fail', reason='the real page check failed')
    data['counts'].update({'pass': 0, 'fail': 2})
    data['gate'] = 'FAIL'
    page = Report(report_html.render(data))
    text = ''.join(page.text)
    assert '1 plant caught' in text and '1 fail' in text
    assert 'the product is wrong' in text
    assert any('fail' in classes and label.lower() == 'fail' for classes, label in page.pills)


def test_caught_plant_plan_and_its_named_check_are_green(tmp_path):
    html = report_html.render(run(tmp_path, js='the price is 12'))
    assert '<li class="plan-item fail">' not in html
    assert '<strong>CAUGHT:</strong>' in html


def test_real_failure_with_an_unrelated_grade_is_not_a_caught_plant(tmp_path):
    data = run(tmp_path, js='the price is 12')
    data['scenarios'][0].update(outcome='fail', graded='pass')
    data['gate'] = 'FAIL'
    page = Report(report_html.render(data))
    real = [row for row in page.pill_contexts if row['section'] == 's0']
    assert len(real) == 1 and real[0]['text'].lower() == 'fail'
    assert 'fail' in real[0]['classes']
    assert 'the product is wrong' in ''.join(page.text)
