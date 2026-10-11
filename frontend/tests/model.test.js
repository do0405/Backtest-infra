import { describe, expect, test } from 'vitest';
import { exact, chartCandles, chartLine, filterTrace, selectionPayload, matchSeries, featureMapping } from '../src/model.js';

describe('exact values and chart admission', () => {
  test('preserves trailing precision, big values and absent values in tables', () => {
    expect(exact('9007199254740993.000000000001')).toBe('9007199254740993.000000000001');
    expect(exact('0.100000000000000000001')).toBe('0.100000000000000000001');
    expect(exact(null)).toBe('—');
    expect(exact(false)).toBe('아니요');
    expect(exact(0)).toBe('0');
  });
  test('retains declared grid gaps and refuses nonfinite or malformed chart candles', () => {
    const valid = { session: '2026-01-02', open: '10', high: '12', low: '9', close: '11' };
    expect(chartCandles([valid, { ...valid, session: '2026-01-06', close: 'Infinity' }], ['2026-01-02', '2026-01-05', '2026-01-06']))
      .toEqual([{ time: '2026-01-02', open: 10, high: 12, low: 9, close: 11 }, { time: '2026-01-05' }, { time: '2026-01-06' }]);
    expect(chartCandles([{ ...valid, low: '20' }], ['2026-01-02'])).toEqual([{ time: '2026-01-02' }]);
  });
  test('chart nulls and unsafe dates become gaps rather than zero or synthetic observations', () => {
    expect(chartLine([{ session: '2026-01-02', nav: null }, { session: '2026-01-03', nav: 'NaN' }, { session: 'bad', nav: '8' }, { session: '2026-01-04', nav: '3.25' }], 'nav'))
      .toEqual([{ time: '2026-01-02' }, { time: '2026-01-03' }, { time: '2026-01-04', value: 3.25 }]);
  });
});

describe('trace stage boundaries', () => {
  const rows = [
    { session: '2026-01-02', security_id: 'b', selected: true, candidate_selected: false, actual_holding_quantity: 0, actual_fills: [] },
    { session: '2026-01-02', security_id: 'a', selected: false, candidate_selected: true, actual_holding_quantity: 5, actual_fills: [{ quantity: 5 }], pending_target: { status: 'queued_next_open' } },
    { session: '2026-01-03', security_id: 'a', selected: true, candidate_selected: true, actual_holding_quantity: 0, actual_fills: [] },
  ];
  test('filters the exact decision date without inferring holdings from selected targets', () => {
    expect(filterTrace(rows, '2026-01-02', 'target').map(row => row.security_id)).toEqual(['b']);
    expect(filterTrace(rows, '2026-01-02', 'held').map(row => row.security_id)).toEqual(['a']);
    expect(filterTrace(rows, '2026-01-02', 'candidate').map(row => row.security_id)).toEqual(['a']);
    expect(filterTrace(rows, '2026-01-02', 'pending').map(row => row.security_id)).toEqual(['a']);
    expect(filterTrace(rows, '2026-01-02', 'filled').map(row => row.security_id)).toEqual(['a']);
    expect(filterTrace(rows, '2026-01-04', 'all')).toEqual([]);
  });
});

describe('explicit source contracts', () => {
  const raw = { security_id: 'a', source_contract_id: 'c', basis: 'as_traded', feed: 'f', venue: 'v', trade_scope: 'all', currency: 'USD', quantity_unit: 'share', series_key: 'raw', source: 'provider', adjustments: [], market: 'US' };
  test('submits only registered selection fields without silently modifying basis', () => {
    expect(selectionPayload({ ...raw, name: 'Name', historical_identity: 'unknown' })).toEqual({ security_id: 'a', source_contract_id: 'c', basis: 'as_traded', feed: 'f', venue: 'v', trade_scope: 'all', currency: 'USD', quantity_unit: 'share', series_key: 'raw' });
    expect(matchSeries(raw, { ...raw, feed: 'other' })).toBe(false);
    expect(matchSeries(raw, { ...raw, series_key: undefined })).toBe(true);
  });
  test('requires an explicitly chosen feature series for every raw security', () => {
    const adjusted = { ...raw, series_key: 'adjusted', basis: 'provider_adjusted', adjustments: ['split'] };
    expect(() => featureMapping([raw], {})).toThrow(/지표/);
    expect(featureMapping([raw, adjusted], { raw: 'adjusted' })).toEqual({ raw_series_keys: ['raw'], feature_series_keys: ['adjusted'] });
    expect(() => featureMapping([raw, adjusted], { raw: 'unknown' })).toThrow(/지표/);
    expect(() => featureMapping([raw, { ...raw, series_key: 'raw2' }], { raw: 'raw', raw2: 'raw2' })).toThrow(/원본/);
  });
});
