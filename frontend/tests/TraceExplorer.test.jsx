import React from 'react';
import { afterEach, expect, test } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { TraceExplorer } from '../src/components/TraceExplorer.jsx';

afterEach(cleanup);

test('renders distinct target, pending, fill and holdings stages with exact source references', () => {
  const result = {
    rows: [{ session: '2026-01-02', nav: '1000' }],
    selection_trace: [{ session: '2026-01-02', security_id: 'security-a', raw_series_key: 'raw', feature_series_key: 'adjusted', eligible: true, candidate_selected: true, selected: true, score: '0.123456789000000000001', rank: 1, target_weight: '0.100000000000000000001', actual_holding_quantity: 0, actual_holding_weight: '0', actual_held_sessions: 0, pending_target: { status: 'queued_next_open', signal_session: '2026-01-02', target_weight: '0.100000000000000000001' }, actual_fills: [], decision_reasons: ['momentum_selected'], feature_values: { momentum: '0.123456789000000000001' }, feature_statuses: { momentum: { status: 'available', reason: null } }, feature_input_windows: { momentum: { series_key: 'adjusted', start_session: '2025-01-02', end_session: '2026-01-02', observation_count: 2, declared_session_count: 252, input_refs_hash: 'window-hash' } }, input_observation_refs: [{ session: '2026-01-02', observation_id: 'ref-a', input_hash: 'ref-hash' }], execution_events: [] }],
    input_observation_catalog: [{ series_key: 'adjusted', session: '2025-01-02', observation_id: 'full-ref', input_hash: 'full-hash' }],
    series_identities: { raw: { security_id: 'security-a', source: 'source', basis: 'as_traded', currency: 'USD' } },
  };
  render(<TraceExplorer result={result} session="2026-01-02" onSessionChange={() => {}} names={{ 'security-a': '실제 종목' }} />);
  expect(screen.getAllByText('0.100000000000000000001').length).toBeGreaterThan(0);
  expect(screen.getAllByText('0.123456789000000000001').length).toBeGreaterThan(0);
  expect(screen.getByText('실제 종목')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: /^실제 보유/ }));
  expect(screen.getByText('이 단계에 해당하는 종목이 없습니다.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: /^대기 주문/ }));
  expect(screen.getByText('실제 종목')).toBeTruthy();
  fireEvent.click(screen.getByText('판단 근거와 원본 입력 보기'));
  expect(screen.getByText('full-ref')).toBeTruthy();
  expect(screen.getByText(/경계와 현재 관측/)).toBeTruthy();
});
