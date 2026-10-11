import React from 'react';
import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { AnalysisReport } from '../src/components/AnalysisReport.jsx';

vi.mock('../src/components/Charts.jsx', () => ({ ResearchChart: props => <div aria-label="comparison chart" data-benchmark={JSON.stringify(props.benchmarkRows)} /> }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const id = 'a'.repeat(64), other = 'b'.repeat(64), series = 'c'.repeat(64);
const result = { result_id: id, dataset_id: 'd'.repeat(64), rows: [{ session: '2026-09-01', nav: '100' }], execution_profile: { market: 'KR', currency: 'KRW' } };
const selections = [{ series_key: series, security_id: 'security-a', market: 'KR', currency: 'KRW', source: 'kis', basis: 'as_traded', adjustments: [] }];
const report = identifier => ({ result_id: identifier, dataset_id: result.dataset_id, analysis_id: 'e'.repeat(64),
  trades: { summary: { closed_portion_count: 2, open_quantity: 3, realized_net_pnl: '33.40', win_rate: { value: .5 }, profit_factor: { value: 4 } }, closed_portions: [], open_lots: [] },
  benchmark: { status: 'not_selected', reason: 'benchmark_not_selected' }, comparison: { total_return_difference: { value: null, reason: 'benchmark_not_selected' } } });

test('explicit benchmark selection and immutable download links show separate costs and closed portions', async () => {
  const fetch = vi.fn(async path => {
    const body = report(id);
    body.risk = { sortino: { value: '0.123456789', reason: null }, calmar: { value: null, reason: 'zero_drawdown' }, definitions: { annualization_sessions: 252 } };
    if (path.includes('benchmark_series_key=')) body.benchmark = { status: 'incomplete', reason: 'incomplete_benchmark_sessions', rows: [{ session: '2026-09-01', nav: null }], missing_sessions: ['2026-09-01'], series_identity: selections[0] };
    return { ok: true, json: async () => body };
  });
  vi.stubGlobal('fetch', fetch);
  render(<AnalysisReport result={result} selections={selections} names={{ 'security-a': '삼성전자' }} />);
  await screen.findByText('33.40');
  expect(screen.getByLabelText('가격 비교 기준').value).toBe('');
  expect(screen.getByText(/분할 청산은 각각의 매입 물량 부분/)).toBeTruthy();
  expect(screen.getByText('소르티노 비율 · 목표 0')).toBeTruthy();
  expect(screen.getByText('0.123456789')).toBeTruthy();
  expect(screen.getByText('낙폭 0 · 분모 없음')).toBeTruthy();
  expect(screen.getByRole('link', { name: '판단·선정 CSV' }).getAttribute('href')).toBe(`/api/results/${id}/export?format=decisions`);
  expect(screen.getByRole('link', { name: '고정 입력 JSON' }).getAttribute('href')).toBe(`/api/results/${id}/export?format=dataset`);
  fireEvent.change(screen.getByLabelText('가격 비교 기준'), { target: { value: series } });
  await screen.findByText(/가격 기준 자료 누락/);
  expect(fetch.mock.calls.at(-1)[0]).toContain(`benchmark_series_key=${series}`);
  expect(screen.getByText(/배당 총수익이나 실제 매수·보유 체결/)).toBeTruthy();
  expect(screen.getByRole('link', { name: '분석 보고서 JSON' }).getAttribute('href')).toContain(`benchmark_series_key=${series}`);
});

test('changing result clears stale analysis and rejects a report bound to another result', async () => {
  const fetch = vi.fn(async path => ({ ok: true, json: async () => report(id) }));
  vi.stubGlobal('fetch', fetch);
  const view = render(<AnalysisReport result={result} selections={selections} />);
  await screen.findByText('33.40');
  view.rerender(<AnalysisReport result={{ ...result, result_id: other }} selections={selections} />);
  await waitFor(() => expect(screen.queryByText('33.40')).toBeNull());
  await screen.findByRole('alert');
  expect(screen.getByRole('link', { name: '결과 JSON' }).getAttribute('href')).toContain(other);
});

test('missing initial anchor explains normalization separately from actual absent prices', async () => {
  const body = report(id);
  body.benchmark = { status: 'unavailable', reason: 'benchmark_initial_anchor_missing',
    normalization_reason: 'benchmark_initial_anchor_missing', missing_sessions: ['2020-01-02'],
    normalization_unavailable_sessions: ['2020-01-02', '2020-01-03', '2020-01-06'],
    rows: [{ session: '2020-01-02', nav: null }, { session: '2020-01-03', close: '11', nav: null }], series_identity: selections[0] };
  body.comparison.total_return_difference = { value: null, reason: 'benchmark_initial_anchor_missing' };
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => body })));
  render(<AnalysisReport result={result} selections={selections} />);
  await screen.findByText(/첫 비교 기준일 가격이 없어/);
  const actualMissing = screen.getByText(/^가격 기준 자료 누락:/).textContent;
  expect(actualMissing).toContain('2020-01-02');
  expect(actualMissing).not.toContain('2020-01-03');
  expect(screen.getByText(/이후 날짜의 실제 가격 누락을 뜻하지 않습니다/)).toBeTruthy();
});
