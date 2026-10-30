"""New retention tails fail closed unless their full text is cleaned or their non-text use is audited."""
import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# These cuts select complete records/lines rather than characters. An exact source
# context is required: a new tail in the same function receives no blanket exception.
RECORD_TAILS = {
    ('qajev/app.py', 'AppProcess._exited', 'self.log[-5:]'),
    ('qajev/electron.py', 'ElectronGame.start', 'self.log[-5:]'),
    ('qajev/native.py', 'GodotGame._exited', 'self.log[-5:]'),
    ('qajev/dashboard.py', '_live', 'steps[-8:]'),
    ('qajev/dashboard.py', '_step', "out['history'][-300:]"),
    ('qajev/jobs.py', '_events', 'noise[-5:]'),
    ('qajev/jobs.py', 'decisions', "[e for e in events if e.get('event') == 'decision'][-limit:]"),
    ('qajev/mcp_server.py', 'qa_browser', 'tail[-5:]'),
    ('qajev/mcp_server.py', 'qa_doctor', 'tail[-5:]'),
    ('qajev/mcp_server.py', 'qa_mode', 'tail[-5:]'),
    ('qajev/project.py', 'index_rows', 'rows[-limit:]'),
    ('qajev/runner.py', 'run_scenario', "state['history'][-15:]"),
    ('qajev/runner.py', 'drive', "state['history'][-5:]"),
    ('qajev/native.py', 'play', 'history[-15:]'),
    ('qajev/native.py', 'play_for', 'history[-30:]'),
    ('qajev/signin.py', 'failed_requests', 'failed[-shown:]'),
    ('qcloud/sync.py', 'save_state', 'sorted(sent)[-5000:]'),
}
SANITIZERS = {'sanitize_text', 'sanitize_upload'}


def tail_inventory(source, filename):
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    aliases = {alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               for alias in node.names if alias.name in SANITIZERS} | SANITIZERS
    cached_scopes = {}

    def scopes(node):
        result = []
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                result.append(node)
        return result

    def scope_nodes(scope):
        def walk(node):
            yield node
            if node is not scope and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return
            for child in ast.iter_child_nodes(node):
                yield from walk(child)
        if scope not in cached_scopes:
            cached_scopes[scope] = list(walk(scope))
        return cached_scopes[scope]

    def binding(name, use):
        position = (use.lineno, use.col_offset)
        for scope in scopes(use):
            choices = []
            for node in scope_nodes(scope):
                if (getattr(node, 'lineno', 0), getattr(node, 'col_offset', 0)) >= position:
                    continue
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and (
                        getattr(node, 'end_lineno', 0), getattr(node, 'end_col_offset', 0)) > position:
                    continue
                value = None
                if ((isinstance(node, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == name for t in node.targets))
                        or (isinstance(node, ast.AnnAssign)
                            and isinstance(node.target, ast.Name) and node.target.id == name)):
                    value = node.value
                elif ((isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
                      or (isinstance(node, ast.ImportFrom)
                          and any((a.asname or a.name) == name for a in node.names))
                      or (isinstance(node, ast.arg) and node.arg == name)):
                    value = node
                if value is not None:
                    choices.append((node.lineno, node.col_offset, value))
            if choices:
                return max(choices, key=lambda row: row[:2])[2]
        return None

    def trusted(call):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id not in aliases:
            return False
        bound = binding(call.func.id, call)
        if isinstance(bound, ast.ImportFrom):
            source_ok = ((bound.level == 0 and bound.module in ('qajev_upload_privacy', 'qcloud.privacy'))
                         or (bound.level == 1 and bound.module == 'privacy' and filename.startswith('qcloud/')))
            return source_ok and any(
                alias.name in SANITIZERS and (alias.asname or alias.name) == call.func.id for alias in bound.names)
        return (filename == 'qajev_upload_privacy.py' and isinstance(bound, ast.FunctionDef)
                and bound.name in SANITIZERS)

    def clean(node, active=frozenset()):
        if node in active:
            return False
        active = active | {node}
        if trusted(node):
            return True
        if isinstance(node, ast.Name):
            bound = binding(node.id, node)
            return bound is not None and clean(bound, active)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            return (node.func.attr in ('strip', 'rstrip', 'lstrip', 'decode', 'encode')
                    and clean(node.func.value, active))
        return False

    def tail_bound(node, active=frozenset()):
        if node is None or node in active:
            return False
        active = active | {node}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return True
        if isinstance(node, ast.Name):
            bound = binding(node.id, node)
            return bound is not None and tail_bound(bound, active)
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)
                and isinstance(node.left, ast.Call) and isinstance(node.left.func, ast.Name)
                and node.left.func.id == 'len'):
            return True
        if isinstance(node, ast.BinOp):
            return tail_bound(node.left, active) or tail_bound(node.right, active)
        if isinstance(node, ast.IfExp):
            return tail_bound(node.body, active) or tail_bound(node.orelse, active)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('min', 'max'):
            return any(tail_bound(arg, active) for arg in node.args)
        # Indexing a record list with [-1] does not make the resulting offset negative.
        # Do not infer a sign from unrelated indices, function bodies or annotations.
        return False

    def semantic_command_tail(node, function):
        if filename != 'qajev/session.py' or function != 'Session.command':
            return False
        parent = parents.get(node)
        if not isinstance(parent, ast.Dict) or not isinstance(node.value, ast.Name):
            return False
        stream = node.value.id
        if stream not in ('stdout', 'stderr') or ast.unparse(node.slice) != '-4000:':
            return False
        fields = {key.value: value for key, value in zip(parent.keys, parent.values)
                  if isinstance(key, ast.Constant)}
        safe = fields.get('diagnostic_' + stream)
        return fields.get(stream) is node and trusted(safe) and len(safe.args) >= 2 and (
            isinstance(safe.args[0], ast.Name) and safe.args[0].id == stream
            and isinstance(safe.args[1], ast.Constant) and safe.args[1].value == 4000
            and any(kw.arg == 'tail' and isinstance(kw.value, ast.Constant) and kw.value.value is True
                    for kw in safe.keywords))

    rows = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and trusted(node) and any(
                kw.arg == 'tail' and isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in node.keywords):
            rows.append({'source': filename, 'line': node.lineno, 'function': '',
                         'expression': ast.unparse(node), 'mode': 'shared-helper'})
        if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
            continue
        if node.slice.upper is not None or not tail_bound(node.slice.lower):
            continue
        function = '.'.join(reversed([s.name for s in scopes(node)
                                      if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]))
        expression = ast.unparse(node)
        if clean(node.value):
            mode = 'clean-before-cut'
        elif isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) and (
                node.value.func.attr == 'splitlines' and clean(node.value.func.value)):
            mode = 'clean-complete-lines'
        elif semantic_command_tail(node, function):
            mode = 'private-command-matching-with-safe-diagnostic-copy'
        elif (filename, function, expression) in RECORD_TAILS:
            mode = 'complete-records'
        else:
            mode = 'unclassified'
        rows.append({'source': filename, 'line': node.lineno, 'function': function,
                     'expression': expression, 'mode': mode})
    return rows


def inventory(root=ROOT):
    package = root / ('qcloud' if (root / 'qcloud').is_dir() else 'qajev')
    files = sorted(package.rglob('*.py')) + [root / 'qajev_upload_privacy.py']
    return [row for path in files if path.is_file() and not path.name.startswith(('test_', '.env'))
            for row in tail_inventory(path.read_text(), path.relative_to(root).as_posix())]


def require_classifications(rows):
    for row in rows:
        assert row['mode'] != 'unclassified', (
            f"{row['source']}:{row['line']}: unclassified tail {row['expression']}; "
            'clean complete generated text before cutting, or audit its non-text purpose')
    return rows


class TailPrivacyInventoryTest(unittest.TestCase):
    def test_every_production_tail_is_cleaned_or_has_an_explicit_non_text_classification(self):
        rows = require_classifications(inventory())
        self.assertTrue(rows)
        self.assertTrue(any(row['mode'] == 'shared-helper' for row in rows))
        prefix = 'qcloud/' if (ROOT / 'qcloud').is_dir() else 'qajev/'
        used = {(row['source'], row['function'], row['expression'])
                for row in rows if row['mode'] == 'complete-records'}
        self.assertEqual(used, {entry for entry in RECORD_TAILS if entry[0].startswith(prefix)},
                         'Remove stale record exemptions instead of leaving a future bypass')

    def test_a_new_raw_tail_in_an_existing_or_new_module_fails_closed(self):
        samples = [
            'def failure(text):\n return text[-300:]\n',
            'def failure(text):\n start = -300\n return text[start:]\n',
            'def failure(text):\n return text[len(text) - 300:]\n',
            'from qajev_upload_privacy import sanitize_text\ndef failure(text):\n return sanitize_text(text[-300:])\n',
        ]
        for filename in ('qcloud/dispatcher.py', 'qajev/cli.py', 'future.py'):
            for source in samples:
                with self.subTest(filename=filename, source=source), \
                        self.assertRaisesRegex(AssertionError, 'unclassified tail'):
                    require_classifications(tail_inventory(source, filename))

    def test_cleaning_after_a_cut_or_a_shadowed_helper_is_not_an_exemption(self):
        samples = [
            ('from qajev_upload_privacy import sanitize_text\ndef failure(text):\n'
             ' tail = text[-300:]\n return sanitize_text(tail)\n'),
            ('from qajev_upload_privacy import sanitize_text\ndef failure(text):\n'
             ' sanitize_text = lambda v: v\n clean = sanitize_text(text)\n return clean[-300:]\n'),
            ('from qajev_upload_privacy import sanitize_text\ndef failure(text, sanitize_text):\n'
             ' clean = sanitize_text(text)\n return clean[-300:]\n'),
        ]
        for source in samples:
            with self.subTest(source=source), self.assertRaisesRegex(AssertionError, 'unclassified tail'):
                require_classifications(tail_inventory(source, 'future.py'))

    def test_a_diagnostic_cut_cannot_reuse_the_private_command_matching_exemption(self):
        source = '''from qajev_upload_privacy import sanitize_text
class Session:
 def command(self, stdout):
  return {"stdout": stdout[-4000:], "diagnostic_stdout": sanitize_text(stdout[-4000:], 4000, tail=True)}
'''
        with self.assertRaisesRegex(AssertionError, 'unclassified tail'):
            require_classifications(tail_inventory(source, 'qajev/session.py'))

    def test_complete_cleaning_before_a_new_cut_is_recognized(self):
        source = '''from qajev_upload_privacy import sanitize_text as public_text
def failure(text):
 clean = public_text(text).strip()
 return clean[-300:]
'''
        rows = require_classifications(tail_inventory(source, 'future.py'))
        self.assertEqual([row['mode'] for row in rows], ['clean-before-cut'])

    def test_selecting_a_record_offset_does_not_make_it_a_negative_character_bound(self):
        source = 'def after_marker(lines, marks):\n last = marks[-1]\n return lines[last + 1:]\n'
        self.assertEqual(tail_inventory(source, 'future.py'), [])


if __name__ == '__main__':
    print(json.dumps(require_classifications(inventory()), indent=2))
