import type { Command } from 'commander';
import type { SearchResult } from '@web-tools/toolkit';
import { runTool } from '../run.js';

export function registerSearchCommand(program: Command) {
  program
    .command('search')
    .description('Search the web via SearXNG')
    .argument('<query>', 'Search query')
    .option('-l, --limit <n>', 'Max results (default: 10)', '10')
    .option('-e, --engines <list>', 'Comma-separated engines (e.g. "google,brave")')
    .action(async (query: string, opts: { limit: string; engines?: string }) => {
      const results = (
        await runTool('web_search', {
          query,
          limit: parseInt(opts.limit, 10),
          ...(opts.engines ? { engines: opts.engines } : {}),
        })
      ).data as SearchResult[];

      if (program.opts().json) {
        console.log(JSON.stringify(results, null, 2));
        return;
      }

      if (results.length === 0) {
        console.log('No results found.');
        return;
      }

      for (const [i, r] of results.entries()) {
        console.log(`${i + 1}. ${r.title}`);
        console.log(`   ${r.url}`);
        if (r.description) console.log(`   ${r.description}`);
        console.log();
      }
    });
}
