import React from 'react';
import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { ResearchChart } from '../src/components/Charts.jsx';

const charts = vi.hoisted(() => ({ series: [] }));
vi.mock('lightweight-charts', () => ({
  AreaSeries: 'area', CandlestickSeries: 'candle', LineSeries: 'line', ColorType: { Solid: 'solid' }, LineType: { WithGaps: 'gaps' },
  createSeriesMarkers: () => ({ setMarkers: () => {} }),
  createChart: () => ({ addSeries: (kind, options) => {
    const record = { kind, options }; charts.series.push(record);
    return { setData: data => { record.data = data; } };
  }, subscribeClick: () => {}, timeScale: () => ({ fitContent: () => {} }), remove: () => {} }),
}));
afterEach(() => { cleanup(); charts.series = []; });

test('normalized benchmark overlays NAV without hiding its missing dates', () => {
  render(<ResearchChart kind="nav" rows={[{ session: '2026-09-01', nav: '100' }, { session: '2026-09-02', nav: '110' }]}
    benchmarkRows={[{ session: '2026-09-01', nav: '100' }, { session: '2026-09-02', nav: null }]} />);
  expect(charts.series).toHaveLength(2);
  expect(charts.series[1].kind).toBe('line');
  expect(charts.series[1].data).toEqual([{ time: '2026-09-01', value: 100 }, { time: '2026-09-02' }]);
  expect(screen.queryByRole('alert')).toBeNull();
});

test('price candles retain their original branch and do not receive a NAV benchmark', () => {
  render(<ResearchChart bars={[{ session: '2026-09-01', open: '10', high: '11', low: '9', close: '10' }]} sessions={['2026-09-01']} />);
  expect(screen.queryByRole('alert')).toBeNull();
  expect(charts.series).toHaveLength(1);
  expect(charts.series[0].data[0].close).toBe(10);
});
