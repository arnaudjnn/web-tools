// The one log helper. stderr only: stdout belongs to results (the CLI prints
// them there, and an MCP stdio transport would read stray stdout as protocol).

export function log(...args: unknown[]): void {
  process.stderr.write(
    args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ') + '\n',
  );
}

export const errMsg = (err: unknown): string => (err instanceof Error ? err.message : String(err));
