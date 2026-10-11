import React from 'react';
import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { ResearchChart } from '../src/components/Charts.jsx';

const chartHarness = vi.hoisted(() => ({ instances: [] }));
vi.mock('lightweight-charts', () => ({
  AreaSeries: {}, CandlestickSeries: {}, LineSeries: {}, ColorType: { Solid: 'solid' }, LineType: { WithGaps: 'gaps' },
  createSeriesMarkers: () => ({ setMarkers: () => {} }),
  createChart: (container, options) => {
    const child = document.createElement('div');
    child.style.height = `${options.height}px`;
    container.appendChild(child);
    const measuredHeights = [];
    // Model an auto-size renderer whose child adds pixels after measurement.
    // An unconstrained parent reads that child on the next notification and
    // feeds the increasing height back; a fixed parent keeps measuring itself.
    function notifyResize() {
      const observedHeight = parseFloat(container.style.height || child.style.height);
      measuredHeights.push(observedHeight);
      child.style.height = `${observedHeight + 20}px`;
    }
    chartHarness.instances.push({ container, options, child, measuredHeights, notifyResize });
    return { addSeries: () => ({ setData: () => {} }), subscribeClick: () => {}, timeScale: () => ({ fitContent: () => {} }), remove: () => child.remove() };
  },
}));

afterEach(() => { cleanup(); chartHarness.instances = []; });

test.each([['price', 320], ['nav', 320], ['drawdown', 180]])('%s chart keeps its bounded height through resize feedback and selection changes', (kind, height) => {
  const props = { kind, bars: [{ session: '2026-09-01', open: '10', high: '11', low: '9', close: '10' }], sessions: ['2026-09-01', '2026-09-02'], rows: [{ session: '2026-09-01', nav: '1000', drawdown: '0' }, { session: '2026-09-02', nav: '1010', drawdown: '-0.01' }], selectedSession: '2026-09-01' };
  const view = render(<ResearchChart {...props} />);
  const container = screen.getByRole('img');
  expect(container.style.height).toBe(`${height}px`);
  expect(container.style.maxHeight).toBe(`${height}px`);
  expect(container.style.padding).toBe('0px');
  const instance = chartHarness.instances[0];
  expect(instance.options.autoSize).toBe(true);
  for (let i = 0; i < 20; i += 1) instance.notifyResize();
  view.rerender(<ResearchChart {...props} selectedSession="2026-09-02" />);
  for (let i = 0; i < 20; i += 1) instance.notifyResize();
  expect(new Set(instance.measuredHeights)).toEqual(new Set([height]));
  expect(container.style.height).toBe(`${height}px`);
  expect(chartHarness.instances).toHaveLength(1);
});
