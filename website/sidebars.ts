import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docs: [
    {
      type: 'category',
      label: 'Guides',
      collapsible: false,
      items: ['intro', 'installation', 'channel-mode', 'reply-routing', 'a2ui-surfaces'],
    },
    {
      type: 'category',
      label: 'API Reference',
      collapsible: false,
      items: ['tools', 'configuration'],
    },
  ],
};

export default sidebars;
