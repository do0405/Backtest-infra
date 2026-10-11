import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, expect, test } from 'vitest';

afterEach(() => { document.head.innerHTML = ''; document.body.innerHTML = ''; });

test('data table padding and borders do not leak into chart-owned table layout', () => {
  const style = document.createElement('style');
  style.textContent = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8');
  document.head.appendChild(style);
  document.body.innerHTML = '<div class="table-wrap"><table><thead><tr><th>Data header</th></tr></thead><tbody><tr><td id="data-cell">Exact value</td></tr></tbody></table></div><div class="chart"><table id="chart-table" style="width: 689px; height: 320px"><tbody><tr><td id="chart-cell"><canvas></canvas></td></tr></tbody></table></div>';
  const dataCell = getComputedStyle(document.getElementById('data-cell'));
  expect(dataCell.borderBottomWidth).toBe('1px');
  expect(dataCell.paddingTop).toBe('12px');
  expect(dataCell.paddingLeft).toBe('15px');
  const chartCell = getComputedStyle(document.getElementById('chart-cell'));
  expect(chartCell.borderBottomWidth).not.toBe('1px');
  expect(chartCell.paddingTop).not.toBe('12px');
  expect(chartCell.paddingLeft).not.toBe('15px');
  const chartTable = getComputedStyle(document.getElementById('chart-table'));
  expect(chartTable.borderCollapse).not.toBe('collapse');
  expect(chartTable.width).toBe('689px');
  expect(chartTable.height).toBe('320px');
});
