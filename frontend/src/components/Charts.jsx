import React, { useEffect, useRef, useState } from 'react';
import { AreaSeries, CandlestickSeries, ColorType, LineSeries, LineType, createChart, createSeriesMarkers } from 'lightweight-charts';
import { chartCandles, chartLine } from '../model.js';
import { Empty } from './Shared.jsx';

const emptyData = [];
export function ResearchChart({ kind = 'price', bars = emptyData, sessions = emptyData, rows = emptyData, features, selectedSession, onSessionChange }) {
  const container = useRef(null);
  const chartState = useRef(null);
  const callback = useRef(onSessionChange);
  const [error, setError] = useState('');
  callback.current = onSessionChange;
  useEffect(() => {
    if (!container.current || (kind === 'price' ? !bars.length : !rows.length)) return;
    setError('');
    const chart = createChart(container.current, {
      height: kind === 'drawdown' ? 180 : 320,
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: '#ffffff' }, textColor: '#687684', fontFamily: 'system-ui, sans-serif', attributionLogo: true },
      grid: { vertLines: { color: '#f0f3f4' }, horzLines: { color: '#edf1f2' } },
      rightPriceScale: { borderColor: '#e7ecee' }, timeScale: { borderColor: '#e7ecee', timeVisible: false },
      crosshair: { vertLine: { color: '#577884' }, horzLine: { color: '#577884' } },
    });
    try {
      let main;
      let data;
      if (kind === 'price') {
        main = chart.addSeries(CandlestickSeries, { upColor: '#087f74', downColor: '#c76265', wickUpColor: '#087f74', wickDownColor: '#c76265', borderVisible: false });
        data = chartCandles(bars, sessions);
        main.setData(data);
        if (features?.rows?.length) {
          const sma = chart.addSeries(LineSeries, { color: '#d4a34d', lineWidth: 2, lineType: LineType.WithGaps, title: `SMA ${features.definitions.sma_period}` });
          sma.setData(chartLine(features.rows, 'sma'));
        }
      } else {
        const drawdown = kind === 'drawdown';
        main = chart.addSeries(AreaSeries, { lineColor: drawdown ? '#ba6770' : '#087f74', topColor: drawdown ? '#fae7e8' : '#cde9e3', bottomColor: '#ffffff', lineWidth: 2, lineType: LineType.WithGaps, title: drawdown ? '낙폭 (비율)' : '자산 가치' });
        data = chartLine(rows, drawdown ? 'drawdown' : 'nav');
        main.setData(data);
      }
      const markers = createSeriesMarkers(main, []);
      chartState.current = { chart, main, markers, data };
      chart.subscribeClick(event => {
        if (!event.time) return;
        const day = typeof event.time === 'string' ? event.time : typeof event.time === 'object' ? `${event.time.year}-${String(event.time.month).padStart(2, '0')}-${String(event.time.day).padStart(2, '0')}` : null;
        if (day) callback.current?.(day);
      });
      chart.timeScale().fitContent();
    } catch { setError('이 자료의 수치 또는 날짜로 차트를 그릴 수 없습니다. 원본 수치 표를 확인하세요.'); }
    return () => { chartState.current = null; chart.remove(); };
  }, [kind, bars, sessions, rows, features]);
  useEffect(() => {
    const state = chartState.current;
    if (!state) return;
    const present = state.data.some(point => point.time === selectedSession && (point.close !== undefined || point.value !== undefined));
    state.markers.setMarkers(present ? [{ time: selectedSession, position: 'aboveBar', shape: 'circle', color: '#d4a34d', text: '선택일' }] : []);
  }, [selectedSession, kind, bars, rows, features]);
  const hasData = kind === 'price' ? bars.length > 0 : rows.length > 0;
  if (!hasData) return <Empty title={kind === 'price' ? '가격을 살펴볼 준비' : '전략의 흐름을 확인하세요'}>자료를 로드하고 연구를 실행하면 실제 가격과 자산 가치가 표시됩니다.</Empty>;
  return <div className="chart-block">{error && <p role="alert" className="error-box">{error}</p>}<div ref={container} className="chart" role="img" aria-label={kind === 'price' ? '실제 가격 캔들 및 이동평균 차트. 날짜 선택은 아래 판단일 메뉴로도 가능합니다.' : kind === 'drawdown' ? '날짜별 자산 낙폭 차트' : '날짜별 자산 가치 차트'} /><div className="chart-credit">Charting by <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView</a> · Lightweight Charts™</div></div>;
}
