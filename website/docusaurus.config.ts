import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// Signal code panel: vsDark colors on the design system's ink surface.
const signalCodeTheme = {
  ...prismThemes.vsDark,
  plain: {
    ...prismThemes.vsDark.plain,
    backgroundColor: '#17171b',
  },
};

const config: Config = {
  title: 'Signal MCP',
  tagline: 'Give your AI agents a Signal.',
  favicon: 'img/favicon.svg',

  future: {
    v4: true,
  },

  url: 'https://joestump.github.io',
  baseUrl: '/signal-mcp/',

  organizationName: 'joestump',
  projectName: 'signal-mcp',

  onBrokenLinks: 'throw',

  markdown: {
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  stylesheets: [
    'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap',
  ],

  headTags: [
    {
      tagName: 'link',
      attributes: {rel: 'preconnect', href: 'https://fonts.googleapis.com'},
    },
    {
      tagName: 'link',
      attributes: {
        rel: 'preconnect',
        href: 'https://fonts.gstatic.com',
        crossorigin: 'anonymous',
      },
    },
  ],

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
      logo: {
        alt: 'Signal MCP',
        src: 'img/logo.svg',
      },
      items: [
        {
          to: '/docs/intro',
          label: 'Guides',
          position: 'left',
          activeBaseRegex: 'docs/(intro|installation|channel-mode)',
        },
        {
          to: '/docs/tools',
          label: 'API Reference',
          position: 'left',
          activeBaseRegex: 'docs/(tools|configuration)',
        },
        {
          href: 'https://github.com/joestump/signal-mcp',
          position: 'right',
          className: 'header-github-link',
          'aria-label': 'GitHub repository',
        },
      ],
    },
    footer: {
      style: 'light',
      links: [
        {
          items: [
            {
              label: 'Docs',
              to: '/docs/intro',
            },
            {
              label: 'API',
              to: '/docs/tools',
            },
            {
              label: 'GitHub',
              href: 'https://github.com/joestump/signal-mcp',
            },
          ],
        },
      ],
      copyright: 'Signal MCP — maintained fork · MIT License',
    },
    prism: {
      theme: signalCodeTheme,
      darkTheme: signalCodeTheme,
      additionalLanguages: ['python', 'bash', 'json'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
