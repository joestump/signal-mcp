import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'Signal MCP',
  tagline: 'Send and receive Signal messages from any AI agent',
  favicon: 'img/favicon.ico',

  future: {
    v4: true,
  },

  url: 'https://joestump.github.io',
  baseUrl: '/signal-mcp/',

  organizationName: 'joestump',
  projectName: 'signal-mcp',

  onBrokenLinks: 'throw',
  onBrokenMarkdownLinks: 'warn',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/joestump/signal-mcp/tree/main/website/',
        },
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Signal MCP',
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docs',
          position: 'left',
          label: 'Docs',
        },
        {
          href: 'https://github.com/joestump/signal-mcp',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {
              label: 'Getting Started',
              to: '/docs/intro',
            },
            {
              label: 'Claude Channel',
              to: '/docs/channel-mode',
            },
          ],
        },
        {
          title: 'Community',
          items: [
            {
              label: 'GitHub',
              href: 'https://github.com/joestump/signal-mcp',
            },
            {
              label: 'signal-cli',
              href: 'https://github.com/AsamK/signal-cli',
            },
            {
              label: 'MCP Protocol',
              href: 'https://github.com/mcp-signal/mcp',
            },
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Signal MCP. Built with Docusaurus.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['python', 'bash', 'json'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
