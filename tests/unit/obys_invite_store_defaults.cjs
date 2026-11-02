// Synthetic signup defaults regression. No browser session, network or employee records.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('app/static/apps/obys/index.html', 'utf8');
const source = html.slice(html.indexOf('    function setDefaults('), html.indexOf('    function showToast('));
const select = value => ({value});
const signup = {businessId: select('biz-sungshin'), branch: select('성신여대점')};
const gate = {businessId: select('biz-sungshin'), branch: select('성신여대점')};
const request = {businessId: select('biz-sungshin'), branch: select('성신여대점')};
const ordinary = {businessId: select('biz-sungshin')};
const forms = [signup, gate, request, ordinary];
forms.forEach(form => {form.businessId.form = form;});
const context = vm.createContext({
  els: {monthFilter: select('2026-11'), gateSignupForm: gate, signupForm: signup, employeeJoinRequestForm: request},
  document: {querySelectorAll: query => query === 'select[name="businessId"]' ? forms.map(form => form.businessId) : [], querySelector: () => null},
  selectedSettingsBusinessId: () => 'biz-junghwa',
  selectedBusinessBranches: () => [{name: '중화점'}],
  activeBranches: () => [{name: '중화점'}, {name: '성신여대점'}],
  settings: () => ({businesses: [{id: 'biz-junghwa'}, {id: 'biz-sungshin'}]}),
  businessIdForBranchName: () => '',
  applyContractDefaults() {}, applyIntegrationPreset() {}
});
const joinFormsSource = html.slice(html.indexOf('    function joinWorkplaceForms()'), html.indexOf('    function joinBusinessView('));
vm.runInContext(joinFormsSource + source + ';setDefaults();setDefaults(true)', context);
for (const form of [gate, signup, request]) {
  assert.equal(form.businessId.value, 'biz-sungshin', 'signup company must retain invite selection');
  assert.equal(form.branch.value, '성신여대점');
}
assert.equal(ordinary.businessId.value, 'biz-junghwa', 'ordinary forms retain settings defaults');
console.log('PASS: invite signup company/branch survive repeated unrelated defaults; join request preserved; ordinary defaults retained');
