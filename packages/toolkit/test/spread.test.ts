import { afterEach, describe, expect, it, vi } from 'vitest';

const resolve4 = vi.fn();
const resolve6 = vi.fn();
vi.mock('node:dns/promises', () => ({
  resolve4: (...a: unknown[]) => resolve4(...a),
  resolve6: (...a: unknown[]) => resolve6(...a),
}));

const { pickReplica, resetSpread } = await import('../src/sidecar.js');

afterEach(() => {
  resetSpread();
  resolve4.mockReset();
  resolve6.mockReset();
  resolve4.mockRejectedValue(Object.assign(new Error('nodata'), { code: 'ENODATA' }));
});

describe('pickReplica', () => {
  it('prefers IPv4 replica addresses (the sidecars bind 0.0.0.0)', async () => {
    resolve4.mockResolvedValue(['10.0.0.2', '10.0.0.1']);
    const a = await pickReplica('http://camoufox.railway.internal:8000');
    expect(a.url).toBe('http://10.0.0.1:8000/');
    expect(resolve6).not.toHaveBeenCalled();
  });

  it('spreads concurrent calls over every replica, least in flight first', async () => {
    resolve6.mockResolvedValue(['fd12::2', 'fd12::1']);
    const a = await pickReplica('http://camoufox.railway.internal:8000');
    const b = await pickReplica('http://camoufox.railway.internal:8000');
    expect(new Set([a.url, b.url])).toEqual(
      new Set(['http://[fd12::1]:8000/', 'http://[fd12::2]:8000/']),
    );
    a.release();
    const c = await pickReplica('http://camoufox.railway.internal:8000');
    expect(c.url).toBe(a.url); // the freed replica is the least busy again
  });

  it('keeps the base URL for a single replica, a failed lookup or a non-railway host', async () => {
    resolve6.mockResolvedValueOnce(['fd12::1']);
    expect((await pickReplica('http://camoufox.railway.internal:8000')).url).toBe('http://camoufox.railway.internal:8000');
    resetSpread();
    resolve6.mockRejectedValueOnce(Object.assign(new Error('nx'), { code: 'ENOTFOUND' }));
    expect((await pickReplica('http://camoufox.railway.internal:8000')).url).toBe('http://camoufox.railway.internal:8000');
    expect((await pickReplica('http://localhost:8002')).url).toBe('http://localhost:8002');
  });

  it('caches the lookup and releases at most once', async () => {
    resolve6.mockResolvedValue(['fd12::1', 'fd12::2']);
    const a = await pickReplica('http://camoufox.railway.internal:8000');
    a.release();
    a.release();
    await pickReplica('http://camoufox.railway.internal:8000');
    expect(resolve6).toHaveBeenCalledTimes(1);
  });
});
