import { afterEach, expect, test, vi } from 'vitest';
import { request, loadActualDataset } from '../src/api.js';

afterEach(() => vi.unstubAllGlobals());

test('a failed API request rejects with server error and never fabricates data', async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => ({ error: 'bounded_sql_unavailable' }) });
  vi.stubGlobal('fetch', fetch);
  await expect(request('/api/securities')).rejects.toThrow(/데이터베이스/);
  expect(fetch).toHaveBeenCalledTimes(1);
});

test('a network failure remains an error rather than an empty success', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
  await expect(request('/api/health')).rejects.toThrow(/연결/);
});

test('fixtures and empty input snapshots are rejected from the actual research flow', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ origin: 'test_fixture', bars: [{}], sessions: ['2026-01-02'], dataset_id: 'id', original_target_count: 17758 }) }));
  await expect(loadActualDataset('/api/datasets', { method: 'POST', body: {} })).rejects.toThrow(/실제/);
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ origin: 'actual_source', bars: [], sessions: [], dataset_id: 'id', original_target_count: 17758 }) }));
  await expect(loadActualDataset('/api/datasets/id')).rejects.toThrow(/가격/);
});
