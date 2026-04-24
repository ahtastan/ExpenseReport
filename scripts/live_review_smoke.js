const { chromium } = require('playwright');
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const repoRoot = path.resolve(__dirname, '..');
const pythonExe = process.env.PYTHON || 'C:\\Users\\CASPER\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe';
let baseUrl = process.env.SMOKE_BASE_URL || 'http://127.0.0.1:8090';
const importFixturePath = process.env.SMOKE_IMPORT_FIXTURE || path.join(repoRoot, '.verify_data', 'live_import_statement_smoke.xlsx');

async function dropdownByText(page, currentText, optionText) {
  await page.getByRole('button', { name: new RegExp(currentText) }).first().click();
  await page.getByRole('button', { name: optionText, exact: true }).click();
}

async function waitForServer(url, timeoutMs = 20000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch (_) {
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  }
  throw new Error(`Server did not become ready at ${url}`);
}

function seedIsolatedDb(env) {
  const seedCode = `
from datetime import date
from pathlib import Path
from openpyxl import Workbook
from sqlmodel import Session
from app.db import create_db_and_tables, engine
from app.models import StatementImport, StatementTransaction

create_db_and_tables()
with Session(engine) as session:
    imp = StatementImport(source_filename="live_browser_smoke.xlsx", storage_path="(smoke)", row_count=4)
    session.add(imp)
    session.commit()
    session.refresh(imp)
    rows = [
        (date(2026, 1, 4), "Aat Istanbul Airport S", 550.00),
        (date(2026, 1, 4), "Sbux Ist Otg Poyrazkoy", 220.00),
        (date(2026, 1, 5), "Uber Trip", 415.25),
        (date(2026, 1, 6), "Catirti Tekel", 88.00),
    ]
    for idx, (tx_date, supplier, amount) in enumerate(rows, start=1):
        session.add(StatementTransaction(
            statement_import_id=imp.id,
            transaction_date=tx_date,
            supplier_raw=supplier,
            supplier_normalized=supplier.lower(),
            local_currency="TRY",
            local_amount=amount,
            source_row_ref=f"smoke-{idx}",
        ))
    session.commit()
fixture = Path(".verify_data") / "live_import_statement_smoke.xlsx"
fixture.parent.mkdir(parents=True, exist_ok=True)
wb = Workbook()
ws = wb.active
ws.append(["Tran Date", "Supplier", "Source Amount", "Amount Incl"])
ws.append(["04/01/2026", "Smoke Import Market", "123.45 TRY", 2.85])
wb.save(fixture)
wb.close()
print(f"seeded_db={engine.url}")
`;
  const result = spawnSync(pythonExe, ['-X', 'utf8', '-'], {
    cwd: repoRoot,
    env,
    input: seedCode,
    encoding: 'utf8',
  });
  if (result.status !== 0) {
    throw new Error(`DB seed failed:
status=${result.status}
error=${result.error ? result.error.message : ''}
stdout=${result.stdout || ''}
stderr=${result.stderr || ''}`);
  }
  process.stdout.write(result.stdout);
}

async function withOptionalServer(run) {
  if (process.env.SMOKE_ISOLATED !== '1') return run();

  const verifyDir = path.join(repoRoot, '.verify_data');
  fs.mkdirSync(verifyDir, { recursive: true });
  const suffix = `${Date.now()}_${Math.random().toString(16).slice(2)}`;
  const dbPath = path.join(verifyDir, `live_browser_smoke_${suffix}.db`);
  const env = {
    ...process.env,
    DATABASE_URL: `sqlite:///${dbPath}`,
    EXPENSE_STORAGE_ROOT: verifyDir,
    EXPENSE_REPORT_TEMPLATE_PATH: path.resolve(repoRoot, '..', 'Expense Report Form_Blank.xlsx'),
    PYTHONPATH: 'backend',
    PYTHONIOENCODING: 'utf-8',
    PYTHONDONTWRITEBYTECODE: '1',
  };
  seedIsolatedDb(env);

  const port = process.env.SMOKE_PORT || '8090';
  baseUrl = `http://127.0.0.1:${port}`;
  const server = spawn(pythonExe, ['-X', 'utf8', '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', port], {
    cwd: path.join(repoRoot, 'backend'),
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let serverOutput = '';
  server.stdout.on('data', chunk => { serverOutput += chunk.toString(); });
  server.stderr.on('data', chunk => { serverOutput += chunk.toString(); });

  try {
    await waitForServer(`${baseUrl}/review`);
    return await run();
  } finally {
    server.kill();
    process.stdout.write(`server_output=${serverOutput.replace(/\s+/g, ' ').trim()}\n`);
  }
}

async function smokeBrowser() {
  if (process.env.SMOKE_RAW_CDP === '1') {
    return smokeRawCdp();
  }
  let browser;
  let page;
  if (process.env.SMOKE_CDP_URL) {
    browser = await chromium.connectOverCDP(process.env.SMOKE_CDP_URL);
    const context = browser.contexts()[0] || await browser.newContext({ viewport: { width: 1440, height: 900 } });
    page = await context.newPage();
    await page.setViewportSize({ width: 1440, height: 900 });
  } else {
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  }
  const consoleErrors = [];
  page.on('console', msg => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', err => consoleErrors.push(err.message));

  await page.goto(`${baseUrl}/review`, { waitUntil: 'networkidle' });
  await page.getByPlaceholder('ahmet or deniz').fill('ahmet');
  await page.getByPlaceholder('demo').fill('demo');
  await page.getByRole('button', { name: /sign in/i }).click();

  await page.getByRole('button', { name: /review queue/i }).click();
  await page.getByText('Bulk classify').waitFor({ state: 'visible', timeout: 15000 });
  await page.getByText('Aat Istanbul Airport S').waitFor({ state: 'visible', timeout: 15000 });
  await page.getByRole('button', { name: /confirmed/i }).click();
  await page.getByText('No rows match this filter.').waitFor({ state: 'visible', timeout: 15000 });
  await dropdownByText(page, 'Scope', 'selected (visible)');
  await page.getByText('0 visible rows').waitFor({ state: 'visible', timeout: 15000 });
  const emptyApplyButton = page.getByRole('button', { name: /apply to visible/i });
  await emptyApplyButton.waitFor({ state: 'visible', timeout: 15000 });
  if (!(await emptyApplyButton.isDisabled())) {
    throw new Error('Apply to visible should be disabled when no rows are visible');
  }
  await page.getByRole('button', { name: /All/ }).first().click();
  await dropdownByText(page, 'selected (visible)', 'attention_required');

  await page.getByText('Aat Istanbul Airport S').click();
  await page.getByText('Air Travel Reconciliation').waitFor({ state: 'visible', timeout: 15000 });

  await dropdownByText(page, 'B/P', 'business');
  await dropdownByText(page, 'Category', 'Other');
  await dropdownByText(page, 'Bucket', 'Other');

  const bulkResponse = page.waitForResponse(
    response => response.url().includes('/reviews/report/') && response.url().includes('/bulk-update') && response.status() === 200,
    { timeout: 15000 }
  );
  await page.getByRole('button', { name: /apply to flagged/i }).click();
  const response = await bulkResponse;
  const bulkResult = await response.json();
  await page.getByText(/Bulk updated \d+ rows/).waitFor({ state: 'visible', timeout: 15000 });

  await page.reload({ waitUntil: 'networkidle' });
  await page.getByRole('button', { name: /review queue/i }).click();
  await page.getByText('Bulk classify').waitFor({ state: 'visible', timeout: 15000 });
  await page.getByText('Other', { exact: true }).first().waitFor({ state: 'visible', timeout: 15000 });

  await page.getByRole('button', { name: /validation/i }).click();
  await page.getByText('Report Validation').waitFor({ state: 'visible', timeout: 15000 });
  await page.getByText(/error|ready|Session must be confirmed/i).first().waitFor({ state: 'visible', timeout: 15000 });

  if (consoleErrors.length) {
    throw new Error(`Browser console errors:\n${consoleErrors.join('\n')}`);
  }

  console.log(JSON.stringify({
    status: 'passed',
    bulkUpdatedRows: bulkResult.updated_rows,
    remainingAttentionRows: bulkResult.remaining_attention_rows,
  }));
  await browser.close();
}

async function smokeRawCdp() {
  const wsUrl = process.env.SMOKE_CDP_URL;
  if (!wsUrl || !wsUrl.startsWith('ws://')) {
    throw new Error('SMOKE_RAW_CDP requires SMOKE_CDP_URL=ws://...');
  }
  const socket = new WebSocket(wsUrl);
  let nextId = 1;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result || {});
    }
  });
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });

  function send(method, params = {}, sessionId = undefined, timeoutMs = 10000) {
    const id = nextId++;
    socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`CDP timeout: ${method}`));
      }, timeoutMs);
      pending.set(id, {
        resolve: value => { clearTimeout(timer); resolve(value); },
        reject: error => { clearTimeout(timer); reject(error); },
      });
    });
  }

  const target = await send('Target.createTarget', { url: 'about:blank' });
  const attached = await send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
  const sessionId = attached.sessionId;
  await send('Page.navigate', { url: `${baseUrl}/review` }, sessionId);

  async function evalJs(expression, timeoutMs = 10000) {
    const result = await send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    }, sessionId, timeoutMs);
    if (result.exceptionDetails) {
      const details = result.exceptionDetails;
      const exception = details.exception || {};
      throw new Error(
        exception.description ||
        exception.value ||
        details.text ||
        'Runtime.evaluate failed'
      );
    }
    return result.result?.value;
  }

  const waitFor = (expression, timeoutMs = 15000) => evalJs(`new Promise((resolve, reject) => {
    const started = Date.now();
    const tick = () => {
      try {
        if (${expression}) resolve(true);
        else if (Date.now() - started > ${timeoutMs}) reject(new Error('wait timeout: ${expression.replace(/'/g, "\\'")}'));
        else setTimeout(tick, 100);
      } catch (error) { reject(error); }
    };
    tick();
  })`, timeoutMs + 1000);

  const actionHelpers = `
    (() => {
      window.__smoke = window.__smoke || {};
      window.__smoke.text = (node) => (node && node.textContent || '').replace(/\\s+/g, ' ').trim();
      window.__smoke.byText = (text, selector='*') => Array.from(document.querySelectorAll(selector)).find(el => window.__smoke.text(el).includes(text));
      window.__smoke.clickText = (text, selector='*') => {
        const el = window.__smoke.byText(text, selector);
        if (!el) throw new Error('Missing text: ' + text);
        el.click();
        return window.__smoke.text(el);
      };
      window.__smoke.clickExactText = (text, selector='*') => {
        const el = Array.from(document.querySelectorAll(selector)).find(node => window.__smoke.text(node) === text);
        if (!el) throw new Error('Missing exact text: ' + text);
        el.click();
        return window.__smoke.text(el);
      };
      window.__smoke.setInput = (input, value) => {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        setter.call(input, value);
        input.dispatchEvent(new Event('input', { bubbles: true }));
      };
      window.__smoke.setSelect = (select, value) => {
        const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
        const oldValue = select.value;
        setter.call(select, value);
        if (select._valueTracker) select._valueTracker.setValue(oldValue);
        select.dispatchEvent(new Event('input', { bubbles: true }));
        select.dispatchEvent(new Event('change', { bubbles: true }));
      };
      window.__smoke.pickDropdownAfter = (label, option) => {
        const labelNode = window.__smoke.byText(label);
        if (!labelNode) throw new Error('Missing label: ' + label);
        const container = labelNode.parentElement;
        const button = container.querySelector('button') || labelNode.closest('div').querySelector('button');
        if (!button) throw new Error('Missing dropdown button after: ' + label);
        button.click();
        setTimeout(() => {
          const opt = Array.from(document.querySelectorAll('button')).find(btn => window.__smoke.text(btn) === option);
          if (!opt) throw new Error('Missing dropdown option: ' + option);
          opt.click();
        }, 0);
        return true;
      };
      window.__smoke.pickToolbarDropdown = (index, option) => {
        const labelNode = Array.from(document.querySelectorAll('span')).find(el => window.__smoke.text(el) === 'Bulk classify');
        if (!labelNode) throw new Error('Missing bulk toolbar');
        const toolbar = labelNode.parentElement;
        if (!toolbar) throw new Error('Missing bulk toolbar parent');
        const button = toolbar.querySelectorAll('button')[index];
        if (!button) throw new Error('Missing toolbar dropdown index: ' + index);
        button.click();
        return new Promise((resolve, reject) => {
          const started = Date.now();
          const tick = () => {
            const options = Array.from(document.querySelectorAll('button'));
            const opt = options.find(btn => window.__smoke.text(btn) === option);
            if (opt) {
              opt.click();
              resolve(true);
            } else if (Date.now() - started > 3000) {
              reject(new Error('Missing toolbar dropdown option: ' + option + '; saw=' + options.map(window.__smoke.text).filter(Boolean).join('|')));
            } else {
              setTimeout(tick, 50);
            }
          };
          tick();
        });
      };
      return true;
    })()
  `;

  await waitFor("document.querySelector('input[placeholder=\"ahmet or deniz\"]') || document.body.innerText.includes('Review Queue')");
  await evalJs(actionHelpers);
  const needsLogin = await evalJs("!!document.querySelector('input[placeholder=\"ahmet or deniz\"]')");
  if (needsLogin) {
    await evalJs(`
      (() => {
        const inputs = document.querySelectorAll('input');
        window.__smoke.setInput(inputs[0], 'ahmet');
        window.__smoke.setInput(inputs[1], 'demo');
        window.__smoke.clickText('Sign in', 'button');
        return true;
      })()
    `);
  }
  await waitFor("document.body.innerText.includes('Review Queue')");
  await evalJs("window.__smoke.clickText('Review Queue', 'button')");
  await waitFor("document.body.innerText.includes('Bulk classify') && document.body.innerText.includes('Aat Istanbul Airport S')");
  await evalJs(`
    (async () => {
      window.__smoke.clickText('Add Statement', 'button');
      const started = Date.now();
      while (!document.body.innerText.includes('Add Statement Entry')) {
        if (Date.now() - started > 5000) throw new Error('Add Statement modal did not open');
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      const input = document.querySelector('input[type="file"][accept*="image"]');
      if (!input) throw new Error('Missing Add Statement file input');
      const file = new File(['manual smoke receipt'], 'x.jpg', { type: 'image/jpeg' });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      window.__smoke.clickText('Extract', 'button');
      return true;
    })()
  `);
  await waitFor("document.body.innerText.includes('Extraction finished, but no usable statement fields were found.')", 20000);
  await evalJs(`
    (() => {
      const input = document.querySelector('input[type="file"][accept*="image"]');
      if (!input) throw new Error('Missing Add Statement file input');
      const file = new File(['manual smoke receipt'], 'merchant=Migros_total_419.58TRY_2026-03-11.jpg', { type: 'image/jpeg' });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      window.__smoke.clickText('Extract', 'button');
      return true;
    })()
  `);
  await waitFor("document.body.innerText.includes('Extraction needs review') || document.body.innerText.includes('Extraction filled the statement fields')", 20000);
  const addStatementPrefill = await evalJs(`(() => {
    const buttons = Array.from(document.querySelectorAll('button'));
    const saveButton = buttons.find(btn => /Save statement entry/i.test((btn.textContent || '').trim()));
    if (!saveButton) throw new Error('Missing Save statement entry button');
    const modal = saveButton.closest('div[style*="background"]') || document.body;
    const date = Array.from(document.querySelectorAll('input[type="date"]')).find(input => input.value === '2026-03-11');
    const values = Array.from(document.querySelectorAll('input')).map(input => input.value);
    return {
      hasDate: !!date,
      hasSupplier: values.includes('Migros'),
      hasAmount: values.includes('419.58'),
      hasCurrency: values.includes('TRY'),
    };
  })()`);
  if (!addStatementPrefill.hasDate || !addStatementPrefill.hasSupplier || !addStatementPrefill.hasAmount || !addStatementPrefill.hasCurrency) {
    throw new Error('Add Statement fields were not prefilled: ' + JSON.stringify(addStatementPrefill));
  }
  await evalJs(`(() => {
    const inputs = Array.from(document.querySelectorAll('input'));
    const date = inputs.find(input => input.type === 'date' && input.value === '2026-03-11');
    const supplier = inputs.find(input => input.value === 'Migros');
    const amount = inputs.find(input => input.value === '419.58');
    if (!date || !supplier || !amount) throw new Error('Missing Add Statement fields for validation check');
    window.__smoke.setInput(date, '');
    window.__smoke.setInput(supplier, '');
    window.__smoke.setInput(amount, '');
    window.__smoke.clickText('Save statement entry', 'button');
    return true;
  })()`);
  await waitFor("document.body.innerText.includes('Transaction date is required.') && document.body.innerText.includes('Supplier is required.') && document.body.innerText.includes('Positive amount is required.')");
  await evalJs(`(() => {
    const labelInput = (name) => {
      const label = Array.from(document.querySelectorAll('label')).find(item => (item.textContent || '').includes(name));
      return label ? label.querySelector('input') : null;
    };
    const date = labelInput('Date');
    const supplier = labelInput('Supplier');
    const amount = labelInput('Amount');
    if (!date) throw new Error('Missing editable Add Statement date field');
    window.__smoke.setInput(date, '2026-03-11');
    if (!supplier || !amount) throw new Error('Missing editable Add Statement fields');
    window.__smoke.setInput(supplier, 'Migros Market');
    window.__smoke.setInput(amount, '420.00');
    window.__smoke.clickText('Save statement entry', 'button');
    return true;
  })()`);
  await waitFor("document.body.innerText.includes('Migros Market')", 20000);
  await evalJs("window.__smoke.clickText('Confirmed', 'button')");
  await waitFor("document.body.innerText.includes('No rows match this filter.')");
  await evalJs("window.__smoke.pickToolbarDropdown(0, 'selected (visible)')");
  await waitFor("document.body.innerText.includes('0 visible rows')");
  const applyDisabled = await evalJs(`(() => {
    const button = Array.from(document.querySelectorAll('button')).find(btn => /Apply to visible/i.test((btn.textContent || '').trim()));
    if (!button) throw new Error('Missing apply button');
    return button.disabled === true;
  })()`);
  if (!applyDisabled) {
    throw new Error('Apply to visible should be disabled when no rows are visible');
  }
  await evalJs("window.__smoke.clickText('All', 'button')");
  await evalJs("window.__smoke.pickToolbarDropdown(0, 'attention_required')");
  await evalJs("window.__smoke.pickToolbarDropdown(1, 'business')");
  await evalJs("window.__smoke.pickToolbarDropdown(2, 'Air Travel')");
  await evalJs("window.__smoke.pickToolbarDropdown(3, 'Airfare/Bus/Ferry/Other')");
  await evalJs("window.__smoke.clickText('Apply to flagged', 'button')");
  await waitFor("document.body.innerText.includes('Bulk updated')");
  const bodyTextAfterBulk = await evalJs("document.body.innerText");

  await evalJs(`
    (async () => {
      const latest = await fetch('/statements/latest').then(r => r.json());
      const review = await fetch('/reviews/report/' + latest.id).then(r => r.json());
      const row = review.rows.find(r => (r.confirmed?.supplier || '').includes('Aat Istanbul Airport S'));
      if (!row) throw new Error('Missing seeded air row');
      const patched = await fetch('/reviews/report/rows/' + row.id, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fields: { air_travel_rt_or_oneway: 'RT' } }),
      }).then(r => r.json());
      if (patched.confirmed?.air_travel_rt_or_oneway !== 'RT') {
        throw new Error('RT patch did not persist: ' + JSON.stringify(patched.confirmed));
      }
      location.reload();
      return true;
    })()
  `);
  await waitFor("document.querySelector('input[placeholder=\"ahmet or deniz\"]') || document.body.innerText.includes('Review Queue')");
  await evalJs(actionHelpers);
  const needsReloadLogin = await evalJs("!!document.querySelector('input[placeholder=\"ahmet or deniz\"]')");
  if (needsReloadLogin) {
    await evalJs(`
      (() => {
        const inputs = document.querySelectorAll('input');
        window.__smoke.setInput(inputs[0], 'ahmet');
        window.__smoke.setInput(inputs[1], 'demo');
        window.__smoke.clickText('Sign in', 'button');
        return true;
      })()
    `);
  }
  await waitFor("document.body.innerText.includes('Bulk classify') && document.body.innerText.includes('Aat Istanbul Airport S')");
  await evalJs("window.__smoke.clickExactText('Aat Istanbul Airport S', 'div')");
  await waitFor("document.body.innerText.includes('AIR TRAVEL RECONCILIATION')");
  await waitFor("document.querySelector('[data-testid=\"air-travel-panel\"]')?.querySelectorAll('input[type=\"date\"]').length >= 2");
  await evalJs(`
    (() => {
      const panel = document.querySelector('[data-testid="air-travel-panel"]');
      if (!panel) throw new Error('Missing air travel panel');
      const dateInputs = panel.querySelectorAll('input[type="date"]');
      if (dateInputs.length < 2) throw new Error('Missing return date input');
      window.__smoke.setInput(dateInputs[0], '2026-05-09');
      window.__smoke.setInput(dateInputs[1], '2026-03-30');
      window.__smoke.clickText('Save', 'button');
      return true;
    })()
  `);
  // Return date earlier than travel date is intentionally allowed; save must succeed without the old inline error.
  await waitFor("!document.body.innerText.includes('Return date cannot be before travel date.')");

  await evalJs("window.__smoke.clickText('Validation', 'button')");
  await waitFor("document.body.innerText.includes('Report Validation')");
  const validationBodyText = await evalJs("document.body.innerText");

  if (!fs.existsSync(importFixturePath)) {
    throw new Error(`Missing Import Statement smoke fixture: ${importFixturePath}`);
  }
  await evalJs("window.__smoke.clickText('Review Queue', 'button')");
  await waitFor("document.body.innerText.includes('Bulk classify')");
  await evalJs("window.__smoke.clickText('Import Statement', 'button')");
  await waitFor("document.body.innerText.includes('Import Diners Statement')");
  const documentNode = await send('DOM.getDocument', { depth: -1, pierce: true }, sessionId);
  const fileInputNode = await send('DOM.querySelector', {
    nodeId: documentNode.root.nodeId,
    selector: 'input[type="file"][accept=".xlsx,.xls"]',
  }, sessionId);
  if (!fileInputNode.nodeId) {
    throw new Error('Missing Import Statement file input');
  }
  await send('DOM.setFileInputFiles', { nodeId: fileInputNode.nodeId, files: [importFixturePath] }, sessionId);
  await waitFor("document.body.innerText.includes('live_import_statement_smoke.xlsx')");
  await evalJs("window.__smoke.clickText('Import statement', 'button')");
  await waitFor("document.body.innerText.includes('Imported successfully')", 20000);
  const sawImportSuccess = true;
  await waitFor("document.body.innerText.includes('Smoke Import Market')", 20000);
  const importBodyText = await evalJs("document.body.innerText");
  socket.close();

  console.log(JSON.stringify({
    status: 'passed',
    mode: 'raw-cdp',
    sawBulkToast: /Bulk updated \d+ rows/.test(bodyTextAfterBulk),
    sawValidation: validationBodyText.includes('Report Validation'),
    sawImportSuccess,
    sawImportRow: importBodyText.includes('Smoke Import Market'),
  }));
}

withOptionalServer(smokeBrowser).catch(async err => {
  console.error(err.stack || err.message);
  process.exit(1);
});
