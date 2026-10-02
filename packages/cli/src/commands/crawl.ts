import type { Command } from 'commander';
import { printResult, runTool } from '../run.js';

export function registerCrawlCommand(program: Command) {
  program
    .command('crawl')
    .description('Crawl one or more URLs and extract content')
    .argument('<urls...>', 'URLs to crawl')
    .option('--selector <css>', 'CSS selector to target specific elements')
    .option('--timeout <ms>', 'Per-URL page load timeout in milliseconds')
    .action(async (urls: string[], opts: { selector?: string; timeout?: string }) => {
      const params: Record<string, unknown> = { urls };
      if (opts.selector) params.css_selector = opts.selector;
      if (opts.timeout) params.timeout_ms = parseInt(opts.timeout, 10);

      printResult(await runTool('web_crawl', params));
    });
}
