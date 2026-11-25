/**
 * Accessible, dependency-free dual-handle 0..10 rating range.
 * The public catalogue and Streaming filters share one interaction contract.
 *
 * Each input is a native range: pointer, touch, keyboard and assistive
 * technology work without custom drag geometry or stale intermediate writes.
 */
export function bindRatingRange(prefix, onCommit) {
  const low = document.getElementById(prefix + '-min');
  const high = document.getElementById(prefix + '-max');
  const root = low?.closest('[data-rating-range]');
  const minOutput = document.getElementById(prefix + '-min-value');
  const maxOutput = document.getElementById(prefix + '-max-value');
  if (!root || !low || !high || !minOutput || !maxOutput) {
    throw new Error('Rating range not mounted: ' + prefix);
  }
  if (low.type !== 'range' || high.type !== 'range') {
    throw new Error('Rating controls must use native range inputs');
  }
  const toScore = value => {
    const number = Number(value);
    return Number.isFinite(number)
      ? Math.min(10, Math.max(0, Math.round(number * 2) / 2)) : 0;
  };
  const display = value => Number(value).toLocaleString(undefined, {
    maximumFractionDigits: 1
  });
  const read = () => ({min:toScore(low.value), max:toScore(high.value)});
  function refresh(changed) {
    let {min, max} = read();
    if (min > max) {
      if (changed === 'min') min = max;
      else max = min;
    }
    low.value = String(min);
    high.value = String(max);
    minOutput.textContent = display(min);
    maxOutput.textContent = display(max);
    root.style.setProperty('--range-low', (min * 10) + '%');
    root.style.setProperty('--range-high', (max * 10) + '%');
    low.setAttribute('aria-valuetext', display(min) + ' sur 10');
    high.setAttribute('aria-valuetext', display(max) + ' sur 10');
    // Bring whichever handle is being moved to the foreground when they meet.
    if (changed) {
      low.style.zIndex = changed === 'min' ? '3' : '2';
      high.style.zIndex = changed === 'max' ? '3' : '2';
    }
  }
  low.addEventListener('input', () => refresh('min'));
  high.addEventListener('input', () => refresh('max'));
  low.addEventListener('pointerdown', () => refresh('min'));
  high.addEventListener('pointerdown', () => refresh('max'));
  for (const input of [low, high]) {
    input.addEventListener('change', () => {
      refresh(input === low ? 'min' : 'max');
      onCommit(read());
    });
  }
  const api = {
    get: read,
    set(min, max) {
      const boundedMin = toScore(min);
      const boundedMax = toScore(max);
      low.value = String(Math.min(boundedMin, boundedMax));
      high.value = String(Math.max(boundedMin, boundedMax));
      refresh();
    }
  };
  api.set(0, 10);
  return api;
}
