import React from 'react';
import { afterEach, expect, test } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { SecurityPicker } from '../src/components/SecurityPicker.jsx';

afterEach(cleanup);

test('saved selections update their visible and accessible names when security metadata resolves later', () => {
  const securityId = '3664afb8-0000-0000-0000-000000000000';
  const props = { start: '2026-09-01', end: '2026-10-07', selections: [{ security_id: securityId, series_key: 'raw', source: 'test-source', currency: 'KRW', basis: 'as_traded' }], onSelectionsChange: () => {}, onNames: () => {} };
  const view = render(<SecurityPicker {...props} names={{}} />);
  expect(screen.queryByText('삼성전자')).toBeNull();
  view.rerender(<SecurityPicker {...props} names={{ [securityId]: '삼성전자' }} />);
  expect(screen.getByText('삼성전자')).toBeTruthy();
  expect(screen.getByRole('button', { name: '삼성전자 거래 원본 제거' })).toBeTruthy();
});
