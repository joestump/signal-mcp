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
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },

  themes: ['@docusaurus/theme-mermaid'],

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
    mermaid: {
      // Signal design system: ultramarine nodes on a soft wash, ink text.
      // `theme` picks the mermaid base per color mode; `themeVariables`
      // paints it with the Signal tokens so diagrams match the site.
      theme: {light: 'base', dark: 'base'},
      options: {
        themeVariables: {
          fontFamily:
            "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
          fontSize: '15px',
          // Nodes: Signal wash fill, ultramarine border + accents.
          primaryColor: '#e3e8fe',
          primaryBorderColor: '#3b45fd',
          primaryTextColor: '#17171b',
          secondaryColor: '#cabcf6',
          secondaryBorderColor: '#7c96f5',
          secondaryTextColor: '#17171b',
          tertiaryColor: '#f2f2f5',
          tertiaryBorderColor: '#c9c9d1',
          tertiaryTextColor: '#2b2b31',
          // Edges + labels.
          lineColor: '#7c96f5',
          textColor: '#2b2b31',
          // Labels riding on edges get a legible background.
          edgeLabelBackground: '#ffffff',
          clusterBkg: '#f8f8fb',
          clusterBorder: '#c9d2fb',
        },
      },
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
