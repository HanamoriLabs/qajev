"""Every production cloud route needs an explicit privacy classification, including new call styles."""
import ast
import json
import re
import tempfile
import unittest
from pathlib import Path

import qajev_upload_privacy as privacy

ROOT = Path(__file__).resolve().parents[1]
METHODS = {name.lower(): name for name in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS')}
# This provider credential probe is an exact external protocol, not a request to QAJev Cloud.
EXTERNAL_PROTOCOL_ROUTES = {('GET', 'https://openrouter.ai/api/v1/key')}


def python_routes(source, filename):
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    accounted = set()
    routes = []

    def shape(node, bindings, active=frozenset()):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return ''.join(str(part.value) if isinstance(part, ast.Constant) else '{}' for part in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return shape(node.left, bindings, active) + shape(node.right, bindings, active)
        if isinstance(node, ast.Name) and node.id in bindings and node.id not in active:
            return shape(bindings[node.id], bindings, active | {node.id})
        return '{}'

    def claim(node, bindings, active=frozenset()):
        accounted.update(ast.walk(node))
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id in bindings and child.id not in active:
                claim(bindings[child.id], bindings, active | {child.id})

    def register(method, node, bindings):
        value = shape(node, bindings)
        if '/api/v1/' not in value:
            return
        if (method, value) in EXTERNAL_PROTOCOL_ROUTES:
            claim(node, bindings)
            return
        assert method in METHODS.values(), f'{filename}:{node.lineno}: unresolved API method'
        path = value[value.index('/api/v1/'):].split('?', 1)[0].split('#', 1)[0]
        routes.append((method, path, filename, node.lineno))
        claim(node, bindings)

    def scope(node, inherited):
        local_nodes = []

        def walk(item):
            local_nodes.append(item)
            if item is not node and isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return
            for child in ast.iter_child_nodes(item):
                walk(child)

        walk(node)
        bindings = dict(inherited)
        for item in local_nodes:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        bindings[target.id] = item.value
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.value is not None:
                bindings[item.target.id] = item.value
        for item in local_nodes:
            if item is not node and isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope(item, bindings)
            if isinstance(item, ast.Call):
                name = item.func.attr if isinstance(item.func, ast.Attribute) else getattr(item.func, 'id', '')
                if name in METHODS and item.args:
                    register(METHODS[name], item.args[0], bindings)
                elif name in ('_call', '_once', 'sanitize_request') and len(item.args) >= 2:
                    register(shape(item.args[0], bindings), item.args[1], bindings)
                elif name == 'Request':
                    keywords = {kw.arg: kw.value for kw in item.keywords}
                    endpoint = item.args[0] if item.args else keywords.get('url')
                    method = shape(keywords['method'], bindings) if 'method' in keywords else (
                        'POST' if len(item.args) > 1 or 'data' in keywords else 'GET')
                    if endpoint is not None:
                        register(method, endpoint, bindings)
            # These are provider inputs, sent by the existing model helpers rather than Client.
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id == 'decide_url':
                        register('POST', item.value, bindings)
                    elif (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                          and target.slice.value == 'TEXT_MODEL_BASE_URL'):
                        register('POST', item.value, bindings)
                        value = shape(item.value, bindings)
                        if '/api/v1/' in value:
                            routes.append(('POST', value[value.index('/api/v1/'):] + '/chat/completions',
                                           filename, item.lineno))

    scope(tree, {})
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
            continue
        if '/api/v1/' not in shape(node, {}):
            continue
        parent = parents.get(node)
        if isinstance(parent, (ast.JoinedStr, ast.BinOp)):
            continue
        if isinstance(parent, ast.Expr):
            owner = parents.get(parent)
            if (isinstance(owner, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and owner.body and owner.body[0] is parent):
                continue  # Documentation is not a request.
        assert node in accounted, f'{filename}:{node.lineno}: API expression has no audited outbound use'
    return routes


def inventory(root=ROOT):
    found = []
    package = root / ('qcloud' if (root / 'qcloud').is_dir() else 'qajev')
    for path in sorted(package.rglob('*.py')):
        if path.name.startswith('test_'):
            continue
        found.extend(python_routes(path.read_text(), path.relative_to(root).as_posix()))
    worker = root / 'worker/src/index.ts'
    if worker.is_file():
        source = worker.read_text()
        declarations = len(re.findall(r'\bfunction\s+route\s*\(', source))
        calls = list(re.finditer(r'route\("([A-Z]+)",\s*"([^"]+)"', source))
        assert len(re.findall(r'\broute\s*\(', source)) == declarations + len(calls), (
            'Unrecognized Worker route declaration; classify its method and path explicitly')
        found.extend((method, path, 'worker/src/index.ts', source[:match.start()].count('\n') + 1)
                     for match in calls
                     for method, path in [match.groups()])
        def guarded_methods(fragment):
            return set(re.findall(r'req\.method\s*(?:===|!==)\s*["\']([A-Z]+)["\']', fragment))

        def function_methods(name):
            definition = source
            marker = re.search(r'\bfunction\s+' + re.escape(name) + r'\s*\(', definition)
            if marker is None:
                for names, module in re.findall(r'import\s*\{(.*?)\}\s*from\s*"([^"]+)"', source, re.DOTALL):
                    if re.search(r'\b' + re.escape(name) + r'\b', names) and module.startswith('./'):
                        definition = (worker.parent / (module + '.ts')).read_text()
                        marker = re.search(r'\bfunction\s+' + re.escape(name) + r'\s*\(', definition)
                        break
            assert marker is not None, f'Worker handler has no audited definition: {name}'
            tail = definition[marker.start():]
            boundary = re.search(r'\n(?:export\s+)?(?:async\s+)?function\s+', tail[1:])
            section = tail[:boundary.start() + 1] if boundary else tail
            methods = guarded_methods(section)
            assert methods, f'Worker handler needs an explicit method guard: {name}'
            return methods

        special = list(re.finditer(r'url\.pathname\s*===\s*"(/api/v1/[^"]+)"', source))
        assert len(re.findall(r'url\.pathname\s*===', source)) == len(special), (
            'Unrecognized Worker path comparison; audit the route before adding it')
        prefixes = list(re.finditer(r'url\.pathname\.startsWith\("([^"]+)"\)', source))
        assert len(re.findall(r'url\.pathname\.startsWith\(', source)) == len(prefixes), (
            'Unrecognized Worker path prefix; audit the route before adding it')
        for match in special:
            path = match[1]
            line = source[source.rfind('\n', 0, match.start()) + 1:source.find('\n', match.end())]
            methods = guarded_methods(line)
            tail = source[match.end():]
            if not methods:
                methods = guarded_methods(tail.split('return', 1)[0])
            if not methods:
                handler = re.search(r'return\s+(?:await\s+)?([a-zA-Z_]\w*)\(req', tail[:300])
                assert handler, f'Worker special route has no audited method: {path}'
                methods = function_methods(handler[1])
            found.extend((method, path, 'worker/src/index.ts', source[:match.start()].count('\n') + 1)
                         for method in methods)
        assert 'url.pathname.startsWith("/__/auth/")' in source
        found.extend((method, '/__/auth/*', 'worker/src/index.ts', 0)
                     for method in function_methods('forwardFirebaseAuth'))
        discovered = {row[1] for row in found if row[2] == 'worker/src/index.ts'}
        literals = set(re.findall(r'["\'](/api/v1/[^"\']+)["\']', source))
        assert literals <= discovered, f'Worker API expressions without an audited route: {literals - discovered}'
    return sorted(set(found))


def require_classifications(found):
    assert hasattr(privacy, 'request_mode'), 'Shared route classifications are missing'
    rows = []
    for method, path, file, line in found:
        sample = re.sub(r':[^/]+|\*|\{\}', 'route-fixture', path)
        mode = privacy.request_mode(method, sample)
        assert mode in ('metadata', 'field-input', 'byte-exact'), f'{file}:{line}: unclassified {method} {path}'
        rows.append({'method': method, 'path': path, 'mode': mode, 'source': file, 'line': line})
    return rows


class RoutePrivacyInventoryTest(unittest.TestCase):
    def test_dynamic_worker_route_declarations_require_an_explicit_audit(self):
        worker = ROOT / 'worker/src/index.ts'
        if not worker.is_file():
            self.skipTest('Worker source inventory is exercised in the Cloud repo')
        source = worker.read_text().replace(
            'const ROUTES: Route[] = [',
            'const ROUTES: Route[] = [route("POST", futurePath, JOB, machine),', 1)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'worker/src'
            target.mkdir(parents=True)
            (target / 'index.ts').write_text(source)
            with self.assertRaisesRegex(AssertionError, 'Unrecognized Worker route declaration'):
                inventory(Path(temp))

    def test_unknown_raw_metadata_must_be_json_before_any_transport(self):
        with self.assertRaisesRegex(ValueError, 'UTF-8 JSON'):
            privacy.sanitize_request('POST', '/api/v1/future/status', b'not-json /Users/route-fixture/report')

    def test_every_production_route_has_an_explicit_privacy_classification(self):
        rows = require_classifications(inventory())
        self.assertTrue(rows)
        if (ROOT / 'worker/src/index.ts').is_file():
            server = {(row['method'], row['path']) for row in rows if row['source'] == 'worker/src/index.ts'}
            self.assertEqual(server, {(method, path) for method, path, _ in privacy.REQUEST_ROUTES})

    def test_a_new_route_in_an_existing_or_unknown_client_style_cannot_escape_the_inventory(self):
        samples = [
            'def send(client):\n client.post("/api/v1/future/status", {})\n',
            'def send(alias):\n path = f"/api/v1/future/{1}/status"\n alias._call("POST", path, body={})\n',
        ]
        for source in samples:
            with self.subTest(source=source), self.assertRaisesRegex(AssertionError, 'unclassified'):
                require_classifications(python_routes(source, 'future.py'))
        with self.assertRaisesRegex(AssertionError, 'no audited outbound use'):
            python_routes('custom_sender("/api/v1/future/status", {})', 'future.py')

    def test_registered_byte_exact_inputs_and_cleaned_metadata_remain_in_their_named_groups(self):
        self.assertTrue(hasattr(privacy, 'REQUEST_ROUTES'), 'Shared route inventory is missing')
        body = {'source': '/Users/route-fixture/Work Files/qa.yaml', 'args': ['/dashboard', '/[a-z]+/g']}
        for method, route, mode in privacy.REQUEST_ROUTES:
            path = re.sub(r':[^/]+|\*', 'route-fixture', route)
            with self.subTest(method=method, route=route, mode=mode):
                clean = privacy.sanitize_request(method, path, body)
                raw = json.dumps(body).encode()
                cleaned_raw = privacy.sanitize_request(method, path, raw)
                if mode == 'byte-exact':
                    self.assertEqual(clean, body)
                    self.assertEqual(cleaned_raw, raw)
                else:
                    self.assertNotIn('route-fixture', clean['source'])
                    self.assertNotIn(b'/Users/route-fixture', cleaned_raw)
                    if mode == 'field-input':
                        for field in privacy.EXACT_INPUT_FIELDS.get((method, path), ()):
                            if field in body:
                                self.assertEqual(clean[field], body[field])

