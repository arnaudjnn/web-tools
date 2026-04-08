import type { Command } from 'commander';
import { web_crawl } from '@web-tools/toolkit';

export function registerCrawlCommand(program: Command) {
  program
    .command('crawl')
    .description('Crawl one or more URLs and extract content')
    .argument('<urls...>', 'URLs to crawl')
    .option('--screenshot', 'Capture screenshots')
    .option('--pdf', 'Capture as PDF')
    .option('--magic', 'Enable all anti-bot measures')
    .option('--selector <css>', 'CSS selector to target specific elements')
    .option('--wait-for <css>', 'CSS selector to wait for before extraction')
    .option('--timeout <ms>', 'Page load timeout in milliseconds')
    .action(
      async (
        urls: string[],
        opts: {
          screenshot?: boolean;
          pdf?: boolean;
          magic?: boolean;
          selector?: string;
          waitFor?: string;
          timeout?: string;
        },
      ) => {
        const crawlerConfig: Record<string, unknown> = {};
        if (opts.screenshot) crawlerConfig.screenshot = true;
        if (opts.pdf) crawlerConfig.pdf = true;
        if (opts.magic) crawlerConfig.magic = true;
        if (opts.selector) crawlerConfig.css_selector = opts.selector;
        if (opts.waitFor) crawlerConfig.wait_for = opts.waitFor;
        if (opts.timeout) crawlerConfig.page_timeout = parseInt(opts.timeout, 10);

        const params: Record<string, unknown> = { urls };
        if (Object.keys(crawlerConfig).length > 0) {
          params.crawler_config = crawlerConfig;
        }

        const result = await web_crawl(params);

        if (result.isError) {
          console.error(result.content[0]?.text ?? 'Unknown error');
          process.exit(1);
        }

        for (const c of result.content) {
          if (c.text) console.log(c.text);
        }
      },
    );
}
