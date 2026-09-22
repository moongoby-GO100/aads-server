"""Execute the fill JavaScript against a controlled-input value tracker."""
import asyncio
import json
import shutil
import subprocess

import pytest

from app.browser_bridge.service import BrowserBridgeError, _LocalAgentPage


@pytest.mark.parametrize('tag', ['INPUT', 'TEXTAREA'])
def test_fill_updates_controlled_form_state_without_returning_secret(tag):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to execute the DOM value-tracker regression')
    page = object.__new__(_LocalAgentPage)

    async def command(kind, params):
        assert kind == 'browser_eval'
        # An own-property setter emulates the value tracker in controlled forms.
        harness = r'''
let dom = '', tracked = '', form = '', events = [];
class HTMLInputElement {}
class HTMLTextAreaElement {}
for (const C of [HTMLInputElement, HTMLTextAreaElement]) {
  Object.defineProperty(C.prototype, 'value', {
    get() { return dom; }, set(v) { dom = v; }, configurable: true
  });
}
const el = new (TAG === 'INPUT' ? HTMLInputElement : HTMLTextAreaElement)();
el.tagName = TAG; el.focus = () => {};
Object.defineProperty(el, 'value', {
  get() { return dom; }, set(v) { dom = tracked = v; }
});
el.dispatchEvent = e => {
  events.push(e.type);
  if (e.type === 'input' && dom !== tracked) { form = dom; tracked = dom; }
};
const document = {querySelector: () => el};
const response = eval(EXPR);
if (form !== EXPECTED || events.join(',') !== 'input,change') process.exit(3);
if (JSON.stringify(response).includes(EXPECTED)) process.exit(4);
console.log(JSON.stringify({value: response}));
'''
        code = 'const TAG=' + json.dumps(tag) + ';const EXPR=' + json.dumps(params['expression']) + ';const EXPECTED=' + json.dumps('test-only-secret') + ';\n' + harness
        output = subprocess.run([node, '-e', code], text=True, capture_output=True, check=True)
        return json.loads(output.stdout)

    page._run_browser_command = command
    asyncio.run(page.fill('#password', 'test-only-secret'))


@pytest.mark.parametrize('state', [None, {}, {'filled': False}, 'not-json'])
def test_fill_fails_closed_without_positive_confirmation(state):
    page = object.__new__(_LocalAgentPage)

    async def command(*args):
        return {'value': state}

    page._run_browser_command = command
    with pytest.raises(BrowserBridgeError, match='did not accept'):
        asyncio.run(page.fill('#password', 'test-only-secret'))
