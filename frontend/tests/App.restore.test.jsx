import React from 'react';
import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import App from '../src/App.jsx';

// Canvas is unrelated to snapshot identity; exercise the actual application and
// API request/state flow while replacing only its browser-only chart surface.
vi.mock('../src/components/Charts.jsx', () => ({ ResearchChart: () => <div aria-label="test chart" /> }));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

function snapshotFixture(resultId, datasetId, securityId, seriesKey) {
  const identity = { security_id: securityId, source_contract_id: 'contract', market: 'KR', source: 'test-source', currency: 'KRW', basis: 'as_traded', adjustments: [], feed: 'daily', venue: 'KRX', trade_scope: 'all', volume_basis: 'shares', quantity_unit: 'share' };
  const dataset = { dataset_id: datasetId, origin: 'actual_source', original_target_count: 17758, bars: [{ series: identity, session: '2026-09-01', open: '10', high: '11', low: '9', close: '10' }], sessions: ['2026-09-01'], selected_security_ids: [securityId], cutoff: '2026-10-09T00:00:00+09:00', source_snapshot_at: '2026-10-10T00:00:00Z', quality_gaps: [], calendar_grade: 'provisional_weekdays' };
  const result = { result_id: resultId, dataset_id: datasetId, origin: 'actual_source', original_target_count: 17758, engine: { actual_framework_execution: true }, rows: [{ session: '2026-09-01', nav: '1000', cash: '1000', drawdown: '0', exposure: '0' }], selection_trace: [], series_identities: { [seriesKey]: identity }, raw_series_keys: [seriesKey], feature_series_keys: [seriesKey], run_config: { strategy: 'trend_sma', parameters: {}, initial_cash: '1000', fee_bps: '0', slippage_bps: '0' }, execution_profile: { currency: 'KRW' }, metrics: {}, ledger: [], execution_events: [], quality_gaps: [] };
  return { dataset, result };
}

test('restoring dataset B clears the screener calculated from dataset A before a new ranking is requested', async () => {
  const a = snapshotFixture('a'.repeat(64), 'c'.repeat(64), 'security-a', 'raw-a');
  const b = snapshotFixture('b'.repeat(64), 'd'.repeat(64), 'security-b', 'raw-b');
  const rank = (data, score) => ({ dataset_id: data.dataset.dataset_id, rows: [{ security_id: data.dataset.selected_security_ids[0], session: '2026-09-01', rank: 1, score, basis: 'as_traded', adjustments: [] }], selected_count: 1, original_target_count: 17758, eligible_count: 1, excluded_count: 0, excluded: [], quality_gaps: [] });
  const fetch = vi.fn(async (path, options) => {
    let body;
    if (path === '/api/health') body = { status: 'ready' };
    else if (path === `/api/results/${a.result.result_id}`) body = a.result;
    else if (path === `/api/results/${b.result.result_id}`) body = b.result;
    else if (path === `/api/datasets/${a.dataset.dataset_id}`) body = a.dataset;
    else if (path === `/api/datasets/${b.dataset.dataset_id}`) body = b.dataset;
    else if (path.startsWith('/api/securities')) body = [];
    else if (path.startsWith('/api/features')) body = { rows: [] };
    else if (path.startsWith('/api/publications')) body = [];
    else if (path === '/api/screen') body = JSON.parse(options.body).dataset_id === a.dataset.dataset_id ? rank(a, '987.65432100001') : rank(b, '123.45678900002');
    else throw new Error(`Unexpected request ${path}`);
    return { ok: true, json: async () => body };
  });
  vi.stubGlobal('fetch', fetch);
  window.history.replaceState(null, '', '/');
  render(<App />);
  fireEvent.change(screen.getByLabelText('결과 ID'), { target: { value: a.result.result_id } });
  fireEvent.click(screen.getByRole('button', { name: '저장 결과 불러오기' }));
  await screen.findByText('자산 가치와 낙폭');
  fireEvent.click(screen.getByRole('button', { name: '부분집합 순위' }));
  fireEvent.click(screen.getByRole('button', { name: '순위 계산' }));
  await screen.findByText('987.65432100001');

  fireEvent.change(screen.getByLabelText('결과 ID'), { target: { value: b.result.result_id } });
  fireEvent.click(screen.getByRole('button', { name: '저장 결과 불러오기' }));
  await waitFor(() => expect(screen.getByText(/dddddddddd…dddddd/)).toBeTruthy());
  fireEvent.click(screen.getByRole('button', { name: '부분집합 순위' }));
  expect(screen.queryByText('987.65432100001')).toBeNull();
  expect(screen.getByText('조건을 정하고 비교하세요')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '순위 계산' }));
  await screen.findByText('123.45678900002');
  expect(screen.queryByText('987.65432100001')).toBeNull();
  const requested = fetch.mock.calls.filter(([path]) => path === '/api/screen').map(([, options]) => JSON.parse(options.body).dataset_id);
  expect(requested).toEqual([a.dataset.dataset_id, b.dataset.dataset_id]);
});
