import { describe, expect, it } from 'vitest';
import { count, percent } from './format';

describe('format', () => {
  it('formats counts with grouping, and shows a dash when there is no value', () => {
    expect(count(31906.4)).toBe('31,906');
    expect(count(null)).toBe('—');
  });
  it('formats a share as a whole percent', () => {
    expect(percent(0.226)).toBe('23%');
  });
});
